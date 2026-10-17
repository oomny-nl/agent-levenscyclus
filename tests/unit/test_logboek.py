"""Hashketen, anker en blokkeerstand van het logboek (ASI08, ASI10)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from levenscyclus.logboek import controleer_tegen_anker, ketenhash, lees_anker
from levenscyclus.register.opslag import LogboekFout, Opslag

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
