"""Instellingen uit de omgeving, gedeeld door register, poort, agent, beheer-CLI en demo."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(naam: str, standaard: str) -> str:
    return os.environ.get(naam, standaard)


@dataclass(frozen=True)
class Instellingen:
    """Configuratie van de demo-keten.

    Attributes:
        register_url: Basis-URL van het SCIM-register zoals de containers hem bereiken.
        poort_url: Canonieke URL van de MCP-gateway; ook de `aud` van agent-asserties.
        openfga_url: HTTP-API van OpenFGA.
        openfga_store: Naam van de store; het register maakt haar aan en is er eigenaar van.
        geheimen_dir: Map met lokaal gegenereerde tokens en sleutels (nooit in git).
        db_pad: SQLite-bestand van het register (SCIM-resources, logboek) of van de poort.
        anker_pad: Apart bestand (eigen volume) met de ketenkop van het logboek per record.
        max_skew: Maximale klokafwijking in seconden tussen agent en poort.
        assertie_geldigheid: Maximale levensduur van een agent-assertie in seconden.
        standaard_herbeoordeling: Standaardduur (ISO 8601) van een bevestiging.
        standaard_bevestigingstermijn: Standaardduur (ISO 8601) om na "herbeoordeling vereist"
            te bevestigen.
    """

    register_url: str
    poort_url: str
    openfga_url: str
    openfga_store: str
    geheimen_dir: Path
    db_pad: Path
    anker_pad: Path
    max_skew: int = 5
    assertie_geldigheid: int = 60
    standaard_herbeoordeling: str = "P90D"
    standaard_bevestigingstermijn: str = "P14D"

    @property
    def scim_url(self) -> str:
        """Basis van de SCIM-endpoints."""
        return f"{self.register_url}/scim/v2"

    @classmethod
    def uit_omgeving(cls) -> Instellingen:
        """Lees de instellingen uit de omgeving; standaardwaarden horen bij de compose-keten."""
        return cls(
            register_url=_env("REGISTER_URL", "http://register:8000").rstrip("/"),
            poort_url=_env("POORT_URL", "http://poort:8000/mcp"),
            openfga_url=_env("OPENFGA_URL", "http://openfga:8080").rstrip("/"),
            openfga_store=_env("OPENFGA_STORE", "lindevoort"),
            geheimen_dir=Path(_env("GEHEIMEN_DIR", "/geheimen")),
            db_pad=Path(_env("DB_PAD", "/data/levenscyclus.db")),
            anker_pad=Path(_env("ANKER_PAD", "/anker/logboek-anker.log")),
            max_skew=int(_env("MAX_SKEW", "5")),
            assertie_geldigheid=int(_env("ASSERTIE_GELDIGHEID", "60")),
            standaard_herbeoordeling=_env("STANDAARD_HERBEOORDELING", "P90D"),
            standaard_bevestigingstermijn=_env("STANDAARD_BEVESTIGINGSTERMIJN", "P14D"),
        )
