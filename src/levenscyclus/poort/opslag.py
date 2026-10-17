"""SQLite-opslag van de poort: eenmalige `jti`'s (overleven een herstart) en notities."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS jtis (
    sub TEXT NOT NULL, jti TEXT NOT NULL, verloopt INTEGER NOT NULL, PRIMARY KEY (sub, jti)
);
CREATE TABLE IF NOT EXISTS notities (
    nr INTEGER PRIMARY KEY, dossier TEXT NOT NULL, tekst TEXT NOT NULL,
    agent TEXT NOT NULL, agent_naam TEXT NOT NULL, eigenaar_naam TEXT, tijd TEXT NOT NULL
);
"""


class PoortOpslag:
    """Eén verbinding; de poort draait als één proces.

    Args:
        pad: Databasebestand.
    """

    def __init__(self, pad: Path) -> None:
        pad.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(pad, isolation_level=None, check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    def reserveer_jti(self, sub: str, jti: str, verloopt: int) -> bool:
        """Reserveer `(sub, jti)` atomair; False als die al bestaat (hergebruik).

        Verlopen reserveringen worden bij elke aanroep opgeruimd.
        """
        self._db.execute("DELETE FROM jtis WHERE verloopt < ?", (int(time.time()),))
        try:
            self._db.execute(
                "INSERT INTO jtis (sub, jti, verloopt) VALUES (?, ?, ?)", (sub, jti, verloopt)
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def voeg_notitie_toe(self, dossier: str, tekst: str, agent: dict[str, Any], tijd: str) -> int:
        """Sla een conceptnotitie op namens een agent; geeft het nummer."""
        cursor = self._db.execute(
            "INSERT INTO notities (dossier, tekst, agent, agent_naam, eigenaar_naam, tijd)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (dossier, tekst, agent["id"], agent["naam"], agent.get("eigenaar_naam"), tijd),
        )
        return int(cursor.lastrowid or 0)

    def notities(self, dossier: str) -> list[dict[str, Any]]:
        """Notities van een dossier, op volgorde."""
        rijen = self._db.execute(
            "SELECT * FROM notities WHERE dossier = ? ORDER BY nr", (dossier,)
        ).fetchall()
        return [dict(r) for r in rijen]
