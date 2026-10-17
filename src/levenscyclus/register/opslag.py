"""SQLite-opslag van het register: SCIM-resources, tokens en het geketende logboek."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from levenscyclus.klok import iso
from levenscyclus.logboek import (
    GENESIS,
    Ketencontrole,
    controleer_tegen_anker,
    ketenhash,
    lees_anker,
)
from levenscyclus.register.scim import ScimFout, is_agent

SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    id TEXT PRIMARY KEY, soort TEXT NOT NULL, sleutel TEXT UNIQUE,
    inhoud TEXT NOT NULL, versie INTEGER NOT NULL, aangemaakt TEXT NOT NULL, gewijzigd TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    hash TEXT PRIMARY KEY, principal TEXT NOT NULL, subject TEXT
);
CREATE TABLE IF NOT EXISTS logboek (
    nr INTEGER PRIMARY KEY, record TEXT NOT NULL, vorige TEXT NOT NULL, hash TEXT NOT NULL
);
"""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class LogboekFout(Exception):
    """Het logboek kan niet veilig worden aangevuld (ankerfout of blokkeerstand)."""


class Opslag:
    """Eén SQLite-verbinding; het register draait als één proces met één asyncio-lock.

    Args:
        pad: Databasebestand.
        anker: Ankerbestand op een apart volume.
        basis_url: Externe basis-URL voor `meta.location`.
    """

    def __init__(self, pad: Path, anker: Path | None, basis_url: str) -> None:
        pad.parent.mkdir(parents=True, exist_ok=True)
        self._anker = anker
        if anker is not None:
            anker.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(pad, isolation_level=None, check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self.basis_url = basis_url
        self.geblokkeerd: str | None = None
        self._wachtrij: list[tuple[int, str]] = []
        laatste_anker = lees_anker(anker) if anker is not None else None
        # Kop van de keten volgens het anker: het laatste record dat dit register zelf verankerde.
        self._kop: tuple[int, str] | None = laatste_anker[-1] if laatste_anker else None

    @contextmanager
    def transactie(self) -> Generator[None]:
        """Eén SQLite-transactie; genest hergebruikt de buitenste.

        Logrecords binnen de transactie worden pas na de commit verankerd; de aanroeper krijgt
        pas succes als commit én ankerwrite zijn geslaagd (ONTWERP §4.6).
        """
        if self._db.in_transaction:
            yield
            return
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            self._wachtrij.clear()
            raise
        self._db.execute("COMMIT")
        self._veranker_wachtrij()

    # Resources --------------------------------------------------------------------------------

    @staticmethod
    def _sleutel(soort: str, resource: dict[str, Any]) -> str | None:
        if soort == "User":
            return f"user:{str(resource['userName']).lower()}"
        return f"group:{str(resource['displayName']).lower()}"

    def _meta(self, soort: str, rij: sqlite3.Row) -> dict[str, Any]:
        return {
            "resourceType": soort,
            "created": rij["aangemaakt"],
            "lastModified": rij["gewijzigd"],
            "location": f"{self.basis_url}/scim/v2/{soort}s/{rij['id']}",
            "version": f'W/"{rij["versie"]}"',
        }

    def _rij_naar_resource(self, rij: sqlite3.Row) -> dict[str, Any]:
        resource = json.loads(rij["inhoud"])
        resource["id"] = rij["id"]
        resource["meta"] = self._meta(rij["soort"], rij)
        return resource

    def haal(self, soort: str, id_: str) -> dict[str, Any] | None:
        """Eén resource, of None."""
        rij = self._db.execute(
            "SELECT * FROM resources WHERE id = ? AND soort = ?", (id_, soort)
        ).fetchone()
        return self._rij_naar_resource(rij) if rij else None

    def alle(self, soort: str) -> list[dict[str, Any]]:
        """Alle resources van een soort, op aanmaakvolgorde."""
        rijen = self._db.execute(
            "SELECT * FROM resources WHERE soort = ? ORDER BY aangemaakt, id", (soort,)
        ).fetchall()
        return [self._rij_naar_resource(r) for r in rijen]

    def agents(self) -> list[dict[str, Any]]:
        """Alle agents."""
        return [u for u in self.alle("User") if is_agent(u)]

    def maak(self, soort: str, resource: dict[str, Any], nu: str) -> dict[str, Any]:
        """Sla een nieuwe resource op met servergegenereerde id en meta.

        Raises:
            ScimFout: `409 uniqueness` bij een bestaande userName of displayName.
        """
        id_ = str(uuid.uuid4())
        inhoud = {k: v for k, v in resource.items() if k not in ("id", "meta")}
        try:
            self._db.execute(
                "INSERT INTO resources (id, soort, sleutel, inhoud, versie, aangemaakt, gewijzigd)"
                " VALUES (?, ?, ?, ?, 1, ?, ?)",
                (id_, soort, self._sleutel(soort, resource), json.dumps(inhoud), nu, nu),
            )
        except sqlite3.IntegrityError as fout:
            raise ScimFout(409, f"{soort} met deze naam bestaat al", "uniqueness") from fout
        return self._moet_bestaan(soort, id_)

    def vervang(self, soort: str, id_: str, resource: dict[str, Any], nu: str) -> dict[str, Any]:
        """Vervang de inhoud van een bestaande resource en verhoog de versie."""
        inhoud = {k: v for k, v in resource.items() if k not in ("id", "meta")}
        try:
            self._db.execute(
                "UPDATE resources SET sleutel = ?, inhoud = ?, versie = versie + 1, gewijzigd = ?"
                " WHERE id = ? AND soort = ?",
                (self._sleutel(soort, resource), json.dumps(inhoud), nu, id_, soort),
            )
        except sqlite3.IntegrityError as fout:
            raise ScimFout(409, f"{soort} met deze naam bestaat al", "uniqueness") from fout
        return self._moet_bestaan(soort, id_)

    def _moet_bestaan(self, soort: str, id_: str) -> dict[str, Any]:
        opgeslagen = self.haal(soort, id_)
        if opgeslagen is None:
            raise RuntimeError(f"{soort} {id_} is zojuist geschreven maar niet terug te lezen")
        return opgeslagen

    def verwijder(self, soort: str, id_: str) -> bool:
        """Verwijder een resource; True als er iets weg is."""
        cursor = self._db.execute("DELETE FROM resources WHERE id = ? AND soort = ?", (id_, soort))
        return cursor.rowcount == 1

    # Tokens -----------------------------------------------------------------------------------

    def token_voor(self, principal: str, subject: str | None, pad: Path) -> str:
        """Lever het token van een principal; maak het aan als bestand én hash ontbreken.

        Het bestand in de geheimenmap is wat de client leest; de hash in de database is wat het
        register controleert. Bestaat het bestand al (herstart), dan wordt dat hergebruikt.
        """
        if pad.exists():
            token = pad.read_text(encoding="utf-8").strip()
        else:
            token = secrets.token_urlsafe(32)
            pad.parent.mkdir(parents=True, exist_ok=True)
            pad.write_text(token, encoding="utf-8")
            os.chmod(pad, 0o600)
        self._db.execute(
            "INSERT OR REPLACE INTO tokens (hash, principal, subject) VALUES (?, ?, ?)",
            (_hash(token), principal, subject),
        )
        return token

    def trek_token_in(self, subject: str) -> None:
        """Verwijder de tokens van een eigenaar (bij verwijdering van de gebruiker)."""
        self._db.execute("DELETE FROM tokens WHERE subject = ?", (subject,))

    def principal_van(self, token: str) -> tuple[str, str | None] | None:
        """(principal, subject) bij een token, of None."""
        rij = self._db.execute(
            "SELECT principal, subject FROM tokens WHERE hash = ?", (_hash(token),)
        ).fetchone()
        return (rij["principal"], rij["subject"]) if rij else None

    # Logboek ----------------------------------------------------------------------------------

    def log(self, record: dict[str, Any], tijd: Any) -> dict[str, Any]:
        """Voeg een record toe aan de hashketen en veranker het.

        Args:
            record: Inhoud (zonder `tijd`, `nr`, `vorige`, `hash`).
            tijd: Registertijd (datetime).

        Raises:
            LogboekFout: In de blokkeerstand, of als de ankerwrite mislukt (zet de blokkeerstand).
        """
        if self.geblokkeerd:
            raise LogboekFout(f"logboek geblokkeerd na ankerfout: {self.geblokkeerd}")
        record = {"tijd": iso(tijd), **record}
        laatste = self._db.execute(
            "SELECT nr, hash FROM logboek ORDER BY nr DESC LIMIT 1"
        ).fetchone()
        self._controleer_kop((laatste["nr"], laatste["hash"]) if laatste else None)
        vorige = laatste["hash"] if laatste else GENESIS
        h = ketenhash(vorige, record)
        cursor = self._db.execute(
            "INSERT INTO logboek (record, vorige, hash) VALUES (?, ?, ?)",
            (json.dumps(record, ensure_ascii=False, sort_keys=True), vorige, h),
        )
        nr = int(cursor.lastrowid or 0)
        self._wachtrij.append((nr, h))
        if not self._db.in_transaction:
            self._veranker_wachtrij()
        return {"nr": nr, **record, "vorige": vorige, "hash": h}

    def _controleer_kop(self, laatste: tuple[int, str] | None) -> None:
        """Weiger een append als de database een ander laatste record heeft dan verwacht.

        Verwacht is het laatst gelogde record van de lopende transactie, anders het laatst
        verankerde. Een record dat buiten het register om is toegevoegd, blokkeert het logboek.
        """
        verwacht = self._wachtrij[-1] if self._wachtrij else self._kop
        if laatste != verwacht:
            self.geblokkeerd = f"ketenkop {laatste} wijkt af van het verwachte record {verwacht}"
            raise LogboekFout(
                f"{self.geblokkeerd}; het register weigert verdere appends tot herstart"
            )

    def _veranker_wachtrij(self) -> None:
        """Schrijf de ankers van alle zojuist gecommitte records met één fsync."""
        regels, self._wachtrij = self._wachtrij, []
        if not regels:
            return
        if self._anker is None:
            self._kop = regels[-1]
            return
        try:
            with self._anker.open("a", encoding="utf-8") as bestand:
                bestand.writelines(f"{nr} {h}\n" for nr, h in regels)
                bestand.flush()
                os.fsync(bestand.fileno())
            self._kop = regels[-1]
        except OSError as fout:
            self.geblokkeerd = str(fout)
            raise LogboekFout(
                f"ankerwrite mislukt ({fout}); het register weigert verdere appends tot herstart"
            ) from fout

    def verifieer_bij_start(self) -> Ketencontrole:
        """Controleer keten en anker vóór het register verzoeken aanneemt.

        Elke afwijking weigert te starten, ook een niet-verankerde staart: de keten kan een crash
        tussen commit en ankerwrite niet onderscheiden van een correct gehasht, aangehangen record
        (ONTWERP §4.6). Het register verankert daarom nooit zelf achteraf.

        Raises:
            LogboekFout: Als keten of anker niet kloppen of records niet verankerd zijn.
        """
        records = self.logboek()
        anker = lees_anker(self._anker) if self._anker is not None else None
        if self._anker is not None and records and not anker:
            raise LogboekFout("logboek heeft records maar het anker is leeg; niet te verifiëren")
        uitkomst = (
            controleer_tegen_anker(records, anker) if anker else Ketencontrole(True, "intact")
        )
        if not uitkomst.intact:
            raise LogboekFout(f"logboek bij start niet intact: {uitkomst.reden}")
        if uitkomst.niet_verankerd:
            raise LogboekFout(
                f"{uitkomst.niet_verankerd} logboekrecord(s) niet verankerd; het register start "
                "niet tot een beheerder de staart heeft beoordeeld (ONTWERP §4.6)"
            )
        self._kop = (records[-1]["nr"], records[-1]["hash"]) if records else None
        return uitkomst

    def logboek(self) -> list[dict[str, Any]]:
        """Alle records in volgorde."""
        rijen = self._db.execute("SELECT * FROM logboek ORDER BY nr").fetchall()
        return [
            {"nr": r["nr"], **json.loads(r["record"]), "vorige": r["vorige"], "hash": r["hash"]}
            for r in rijen
        ]
