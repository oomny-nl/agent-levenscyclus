"""Hashketen, anker en blokkeerstand van het logboek (ASI08, ASI10)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from levenscyclus.klok import parse_tijd
from levenscyclus.logboek import controleer_tegen_anker, ketenhash, lees_anker
from levenscyclus.register.app import STAPPEN
from levenscyclus.register.opslag import LogboekFout, Opslag
from tests.unit.conftest import Register

NU = datetime(2026, 10, 17, tzinfo=UTC)


def _opslag(tmp_path: Path) -> Opslag:
    return Opslag(
        tmp_path / "register.db", tmp_path / "anker" / "anker.log", "http://register.test"
    )


def _vul(opslag: Opslag, aantal: int = 3) -> None:
    for i in range(aantal):
        opslag.log({"soort": "levenscyclus", "gebeurtenis": f"stap-{i}"}, NU)


def _db(tmp_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(tmp_path / "register.db")


def test_manipulatie_zichtbaar(tmp_path: Path) -> None:
    opslag = _opslag(tmp_path)
    _vul(opslag)
    records = opslag.logboek()
    anker = lees_anker(tmp_path / "anker" / "anker.log")
    assert controleer_tegen_anker(records, anker).intact
    records[1]["gebeurtenis"] = "iets anders"
    uitkomst = controleer_tegen_anker(records, anker)
    assert (uitkomst.intact, uitkomst.fout_record) == (False, 2)


def test_anker_vangt_weggelaten_staart(tmp_path: Path) -> None:
    opslag = _opslag(tmp_path)
    _vul(opslag)
    anker = lees_anker(tmp_path / "anker" / "anker.log")
    records = opslag.logboek()
    # Zonder anker zou een ingekorte keten intact lijken.
    uitkomst = controleer_tegen_anker(records[:-1], anker)
    assert (uitkomst.intact, uitkomst.fout_record) == (False, 3)
    assert not controleer_tegen_anker([], anker).intact


def _hang_record_aan(tmp_path: Path) -> None:
    """Correct gehasht record rechtstreeks in SQLite, zonder anker (crash of schrijftoegang)."""
    with _db(tmp_path) as db:
        vorige = db.execute("SELECT hash FROM logboek ORDER BY nr DESC LIMIT 1").fetchone()[0]
        record = {"soort": "levenscyclus", "gebeurtenis": "aangehangen", "tijd": "2026-10-17"}
        db.execute(
            "INSERT INTO logboek (record, vorige, hash) VALUES (?, ?, ?)",
            (json.dumps(record, sort_keys=True), vorige, ketenhash(vorige, record)),
        )


def test_niet_verankerde_staart_geteld(tmp_path: Path) -> None:
    opslag = _opslag(tmp_path)
    _vul(opslag)
    _hang_record_aan(tmp_path)
    uitkomst = controleer_tegen_anker(
        opslag.logboek(), lees_anker(tmp_path / "anker" / "anker.log")
    )
    assert (uitkomst.intact, uitkomst.niet_verankerd) == (True, 1)


def test_herstart_weigert_onverankerde_staart(tmp_path: Path) -> None:
    opslag = _opslag(tmp_path)
    _vul(opslag)
    opslag.verifieer_bij_start()
    _hang_record_aan(tmp_path)
    herstart = _opslag(tmp_path)
    with pytest.raises(LogboekFout, match="niet verankerd"):
        herstart.verifieer_bij_start()
    # Het register verankert nooit zelf achteraf.
    assert len(lees_anker(tmp_path / "anker" / "anker.log") or []) == 3


def test_herstart_weigert_log_zonder_anker(tmp_path: Path) -> None:
    opslag = _opslag(tmp_path)
    _vul(opslag)
    (tmp_path / "anker" / "anker.log").write_text("", encoding="utf-8")
    with pytest.raises(LogboekFout, match="anker is leeg"):
        _opslag(tmp_path).verifieer_bij_start()


def test_ankerfout_blokkeert_appends(tmp_path: Path) -> None:
    opslag = _opslag(tmp_path)
    _vul(opslag, 1)
    anker = tmp_path / "anker" / "anker.log"
    anker.unlink()
    anker.mkdir()  # elke append faalt nu met een OSError
    with pytest.raises(LogboekFout, match="ankerwrite mislukt"):
        opslag.log({"soort": "levenscyclus", "gebeurtenis": "zonder anker"}, NU)
    assert opslag.geblokkeerd
    anker.rmdir()
    # Ook als het anker weer schrijfbaar is, blijft het register dicht tot een herstart.
    with pytest.raises(LogboekFout, match="geblokkeerd"):
        opslag.log({"soort": "levenscyclus", "gebeurtenis": "na herstel"}, NU)


def test_aangehangen_record_blokkeert_volgende_append(tmp_path: Path) -> None:
    # Tijdens bedrijf: een correct gehasht record buiten het register om in SQLite gezet. De
    # volgende legitieme append mag daar niet op voortbouwen en het zo alsnog laten verankeren.
    opslag = _opslag(tmp_path)
    _vul(opslag)
    _hang_record_aan(tmp_path)
    with pytest.raises(LogboekFout, match="ketenkop"):
        opslag.log({"soort": "levenscyclus", "gebeurtenis": "legitiem"}, NU)
    assert opslag.geblokkeerd
    assert len(lees_anker(tmp_path / "anker" / "anker.log") or []) == 3


def test_gat_in_anker_afgekeurd(tmp_path: Path) -> None:
    # Voortgebouwd op een tussengevoegd record: het anker mist dan een nummer vóór zijn laatste.
    opslag = _opslag(tmp_path)
    _vul(opslag, 4)
    anker = tmp_path / "anker" / "anker.log"
    regels = anker.read_text(encoding="utf-8").splitlines()
    anker.write_text("\n".join(regels[:2] + regels[3:]) + "\n", encoding="utf-8")
    uitkomst = controleer_tegen_anker(opslag.logboek(), lees_anker(anker))
    assert (uitkomst.intact, uitkomst.fout_record) == (False, 3)


def _weiger(db: sqlite3.Connection, *opdrachten: str) -> None:
    """Laat SQLite deze transactieopdrachten weigeren, zoals bij een volle schijf of lock."""

    def autorisatie(actie: int, arg1: str | None, *_: object) -> int:
        if actie == sqlite3.SQLITE_TRANSACTION and arg1 in opdrachten:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    db.set_authorizer(autorisatie)


def test_mislukte_commit_rolt_terug_en_de_volgende_append_wordt_verankerd(tmp_path: Path) -> None:
    opslag = _opslag(tmp_path)
    _vul(opslag, 1)
    _weiger(opslag._db, "COMMIT")
    with pytest.raises(sqlite3.DatabaseError), opslag.transactie():
        opslag.log({"soort": "toolcall", "beslissing": "toegestaan"}, NU)
    # Geen half open transactie en geen achtergebleven ankers: de toolcall bestaat niet.
    assert not opslag._db.in_transaction
    assert opslag.geblokkeerd is None
    assert [r["gebeurtenis"] for r in opslag.logboek()] == ["stap-0"]
    opslag._db.set_authorizer(None)
    # Een append buiten een transactie (zoals `POST /logboek`) wordt weer direct verankerd.
    opslag.log({"soort": "levenscyclus", "gebeurtenis": "daarna"}, NU)
    anker = lees_anker(tmp_path / "anker" / "anker.log")
    assert [nr for nr, _ in anker or []] == [1, 2]
    uitkomst = controleer_tegen_anker(opslag.logboek(), anker)
    assert (uitkomst.intact, uitkomst.niet_verankerd) == (True, 0)


def test_transactie_die_niet_terug_te_rollen_is_blokkeert_het_logboek(tmp_path: Path) -> None:
    opslag = _opslag(tmp_path)
    _vul(opslag, 1)
    _weiger(opslag._db, "COMMIT", "ROLLBACK")
    with pytest.raises(sqlite3.DatabaseError) as info, opslag.transactie():
        opslag.log({"soort": "toolcall", "beslissing": "toegestaan"}, NU)
    assert opslag._db.in_transaction
    assert opslag.geblokkeerd
    assert any("weigert verdere appends" in n for n in info.value.__notes__)
    # Niets belandt nog in de open transactie: niet als append, niet als nieuwe transactie.
    with pytest.raises(LogboekFout, match="geblokkeerd"):
        opslag.log({"soort": "levenscyclus", "gebeurtenis": "erachteraan"}, NU)
    with pytest.raises(LogboekFout, match="weigert verdere appends"), opslag.transactie():
        pytest.fail("een nieuwe transactie mag niet in de open transactie beginnen")
    assert len(lees_anker(tmp_path / "anker" / "anker.log") or []) == 1


async def test_na_een_mislukte_commit_gaat_het_volgende_verzoek_duurzaam(
    register: Register,
) -> None:
    opslag = register.dienst.opslag
    _weiger(opslag._db, "COMMIT")
    with pytest.raises(sqlite3.DatabaseError):
        await register.verzoek("POST", "/scim/v2/Users", "bron", {"userName": "mislukt"})
    assert register.dienst.opslag.alle("User") == []
    opslag._db.set_authorizer(None)
    await register.mens("lukt")
    records = opslag.logboek()
    assert [r["resource"] for r in records if r["soort"] == "scim"] == ["User lukt"]
    anker = lees_anker(register.dienst.inst.anker_pad)
    assert (anker or [])[-1] == (records[-1]["nr"], records[-1]["hash"])


@pytest.mark.parametrize("veld", ["tijd", "nr", "vorige", "hash"])
def test_log_weigert_velden_die_het_logboek_zelf_zet(tmp_path: Path, veld: str) -> None:
    opslag = _opslag(tmp_path)
    with pytest.raises(ValueError, match=veld):
        opslag.log({"soort": "levenscyclus", veld: "van de client"}, NU)
    assert opslag.logboek() == []


def _toolcall(agent_id: str | None, /, **afwijking: Any) -> dict[str, Any]:
    return {
        "soort": "toolcall",
        "agent": agent_id,
        "agent_naam": "agent-x",
        "eigenaar_naam": None,
        "tool": "dossier_lezen",
        "dossier": "2026-0142",
        "beslissing": "geweigerd",
        "reden": "tool_niet_toegestaan",
        "toelichting": None,
        "controles": {
            "assertie": "ok",
            "levenscyclus": "ok",
            "tool": "geweigerd",
            "relatie": "niet_uitgevoerd",
            "logboek": "ok",
        },
        "recordversie": None,
        "jti": None,
        **afwijking,
    }


def _toolrecords(register: Register) -> list[dict[str, Any]]:
    return [r for r in register.dienst.opslag.logboek() if r["soort"] == "toolcall"]


@pytest.mark.parametrize(
    "afwijking",
    [
        {"tijd": "2000-01-01T00:00:00Z"},
        {"nr": 1},
        {"hash": "0" * 64},
        {"extra": "x"},
        {"soort": "scim"},
        {"tool": None},
        {"agent": 5},
        {"beslissing": "misschien"},
        {"controles": {"assertie": "ok"}},
        {"controles": dict.fromkeys(("assertie", "levenscyclus", "tool", "relatie"), "ok")},
        {"controles": dict.fromkeys(STAPPEN, "prima")},
        {"beslissing": "toegestaan"},
    ],
)
async def test_toolcall_record_buiten_schema_geweigerd(
    register: Register, afwijking: dict[str, Any]
) -> None:
    antwoord = await register.verzoek("POST", "/logboek", "poort", _toolcall(None, **afwijking))
    assert antwoord.status_code == 400, antwoord.text
    assert _toolrecords(register) == []


async def test_toolcall_tijd_komt_van_het_register(register: Register) -> None:
    antwoord = await register.verzoek("POST", "/logboek", "poort", _toolcall(None))
    assert antwoord.status_code == 201, antwoord.text
    [record] = _toolrecords(register)
    assert record["tijd"] == antwoord.json()["tijd"]
    assert parse_tijd(record["tijd"]) <= register.dienst.klok.nu()


async def test_afgewezen_toelating_legt_logboekstap_geweigerd_vast(register: Register) -> None:
    agent = await register.agent(await register.mens("kees"))
    body = _toolcall(
        agent,
        beslissing="toegestaan",
        reden=None,
        controles=dict.fromkeys(STAPPEN, "ok"),
        recordversie='W/"0"',
    )
    antwoord = await register.verzoek("POST", "/logboek", "poort", body)
    assert antwoord.status_code == 409, antwoord.text
    [record] = _toolrecords(register)
    assert record == antwoord.json()
    assert (record["beslissing"], record["reden"], record["toelichting"]) == (
        "geweigerd",
        "toestand_gewijzigd",
        "recordversie verouderd",
    )
    assert record["controles"] == {**dict.fromkeys(STAPPEN, "ok"), "logboek": "geweigerd"}
