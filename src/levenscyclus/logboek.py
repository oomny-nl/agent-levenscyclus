"""Hashketen van het logboek: berekenen, verankeren en controleren.

Elk record bevat de hash van het vorige (SHA-256 over `vorige || canonieke JSON`). Wijzigen of
weglaten van een record breekt de keten. Het anker (nummer en hash per record, op een apart
volume) vangt ook het weglaten van staartrecords en een geleegde tabel. Tamper-evident, niet
tamper-proof: wie log en anker kan herschrijven, kan beide aanpassen.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GENESIS = "0" * 64
TECHNISCH = ("nr", "vorige", "hash")


def canoniek(record: dict[str, Any]) -> str:
    """Canonieke JSON van een record (gesorteerde sleutels, geen witruimte)."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def ketenhash(vorige: str, record: dict[str, Any]) -> str:
    """Hash van een logboekrecord, gekoppeld aan de vorige hash."""
    return hashlib.sha256((vorige + canoniek(record)).encode()).hexdigest()


def controleer_keten(records: list[dict[str, Any]]) -> tuple[bool, int | None]:
    """Controleer de hashketen.

    Args:
        records: Records in volgorde, elk met `nr`, `vorige`, `hash` en de inhoud.

    Returns:
        (True, None) als de keten intact is, anders (False, nummer van het eerste foute record).
    """
    vorige = GENESIS
    for record in records:
        inhoud = {k: v for k, v in record.items() if k not in TECHNISCH}
        if record["vorige"] != vorige or ketenhash(vorige, inhoud) != record["hash"]:
            return False, record["nr"]
        vorige = record["hash"]
    return True, None


def lees_anker(pad: Path) -> list[tuple[int, str]] | None:
    """Ankerregels (nummer, hash); None als het bestand ontbreekt."""
    if not pad.exists():
        return None
    regels = []
    for regel in pad.read_text(encoding="utf-8").splitlines():
        if not regel.strip():
            continue
        nr, h = regel.split()
        regels.append((int(nr), h))
    return regels


@dataclass(frozen=True)
class Ketencontrole:
    """Uitkomst van de controle van keten en anker.

    Attributes:
        intact: True als keten en anker kloppen.
        reden: `intact` of de reden van afkeuring.
        fout_record: Nummer van het eerste foute record, als de keten breekt.
        niet_verankerd: Aantal records na het laatste anker (hoogstens 1 bij een crash tussen
            commit en ankerwrite; meer is in productie een incident).
    """

    intact: bool
    reden: str
    fout_record: int | None = None
    niet_verankerd: int = 0


def _ankerafwijking(
    records: list[dict[str, Any]], anker: list[tuple[int, str]]
) -> Ketencontrole | None:
    hashes = {r["nr"]: r["hash"] for r in records}
    for nr, h in anker:
        if nr not in hashes:
            return Ketencontrole(
                False, f"record {nr} staat in het anker maar ontbreekt in het log", nr
            )
        if hashes[nr] != h:
            return Ketencontrole(False, f"record {nr} wijkt af van het anker", nr)
    return None


def controleer_tegen_anker(
    records: list[dict[str, Any]], anker: list[tuple[int, str]] | None
) -> Ketencontrole:
    """Controleer keten én anker.

    Een log zonder anker is geen bewijs en wordt afgekeurd; alleen een lege omgeving zonder
    records en zonder anker geldt als intact.
    """
    if not records and not anker:
        return Ketencontrole(True, "intact")
    if not anker:
        return Ketencontrole(False, "anker ontbreekt of is leeg; het log is niet te verifiëren")
    intact, fout = controleer_keten(records)
    if not intact:
        return Ketencontrole(False, f"keten gebroken bij record {fout}", fout)
    afwijking = _ankerafwijking(records, anker)
    if afwijking is not None:
        return afwijking
    laatste_anker = anker[-1][0]
    staart = [r["nr"] for r in records if r["nr"] > laatste_anker]
    if staart:
        return Ketencontrole(
            True, f"intact; {len(staart)} record(s) na het anker niet verankerd", None, len(staart)
        )
    return Ketencontrole(True, "intact")
