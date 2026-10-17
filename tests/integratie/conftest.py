"""Gedeelde hulpmiddelen voor de integratietests tegen de echte keten (OpenFGA, register, poort).

De tests draaien in de container `tests` (netwerken `intern` en `front`, geheimen alleen-lezen) met
`make integratie`. Elke test maakt eigen medewerkers en agents met een unieke naam; de teams
`werk-en-inkomen` en `zorg` worden gedeeld, omdat de dossiers van de poort aan die teamnamen
hangen. De registerklok gaat in sommige tests vooruit; tijden worden daarom altijd vanaf de
actuele registertijd berekend.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx2
import pytest

from levenscyclus.agent.verbinding import Weigering
from levenscyclus.assertie import Sleutelpaar
from levenscyclus.beheer import Beheer, BeheerFout, Scim, agentresource, lees_token
from levenscyclus.config import Instellingen
from levenscyclus.klok import iso, parse_tijd
from levenscyclus.poort.gegevens import TEAM_INKOMEN, TEAM_ZORG
from levenscyclus.register.scim import URN_AGENT

INST = Instellingen.uit_omgeving()
ACCEPT = "application/json, text/event-stream"


@dataclass(frozen=True)
class ProefAgent:
    """Een in de test geprovisioneerde agent met zijn sleutelpaar."""

    id: str
    naam: str
    eigenaar: str
    paar: Sleutelpaar


@dataclass(frozen=True)
class Antwoord:
    """Uitkomst van een rauwe `tools/call` op de poort."""

    status: int
    code: str | None
    tekst: str


def uniek(prefix: str) -> str:
    """Unieke `userName`, zodat tests elkaar en de demo niet raken."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@dataclass
class Keten:
    """Clients voor alle rollen, zoals de demoregie ze ook heeft."""

    bron: Scim
    beheerder: Scim
    beheer: Beheer
    beheerder_token: str
    teams: dict[str, str]

    # Tijd en tokens ---------------------------------------------------------------------------

    def registertijd(self) -> datetime:
        """De registerklok (met demo-offset)."""
        return parse_tijd(self.beheer.overzicht()["tijd"])

    def klok(self, vooruit: str) -> None:
        """Zet de registerklok vooruit (`DEMO_KLOK=1` in de compose-keten)."""
        self.beheer.klok(vooruit)

    @staticmethod
    def eigenaar_token(eigenaar_id: str) -> str:
        """Eigenaarstoken uit de geheimenmap, aangemaakt door het register."""
        return lees_token(INST.geheimen_dir / "eigenaren" / f"{eigenaar_id}.token")

    # Mensen en teams --------------------------------------------------------------------------

    def mens(self, team: str | None = TEAM_INKOMEN) -> str:
        """Maak een medewerker aan (optioneel lid van een team); geeft de SCIM-id."""
        naam = uniek("mw")
        user = self.bron.maak("Users", {"userName": naam, "displayName": naam.capitalize()})
        if team is not None:
            self.voeg_toe(team, user["id"])
        return str(user["id"])

    def voeg_toe(self, team: str, gebruiker_id: str) -> None:
        """Maak een medewerker lid van een team (PATCH add)."""
        self.bron.patch(
            "Groups",
            self.teams[team],
            [{"op": "add", "path": "members", "value": [{"value": gebruiker_id}]}],
        )

    def verplaats(self, gebruiker_id: str, van: str, naar: str) -> None:
        """Mover: verwijder uit het ene team en voeg toe aan het andere."""
        self.bron.patch(
            "Groups",
            self.teams[van],
            [{"op": "remove", "path": f'members[value eq "{gebruiker_id}"]'}],
        )
        self.voeg_toe(naar, gebruiker_id)

    def vertrek(self, gebruiker_id: str) -> None:
        """Leaver: de bron zet de medewerker op `active=false`."""
        self.bron.patch(
            "Users", gebruiker_id, [{"op": "replace", "path": "active", "value": False}]
        )

    # Agents -----------------------------------------------------------------------------------

    def agent(
        self,
        eigenaar_id: str,
        tools: tuple[str, ...] = ("dossier_lezen", "notitie_toevoegen"),
        vervaldagen: float = 365,
        **extra: Any,
    ) -> ProefAgent:
        """De beheerder provisioneert een agent; het sleutelpaar ontstaat hier."""
        paar = Sleutelpaar.nieuw()
        naam = uniek("agent")
        vervaldatum = iso(self.registertijd() + timedelta(days=vervaldagen))
        body = agentresource(
            naam,
            eigenaar_id,
            paar.publieke_jwk(),
            {
                "doel": f"integratietest {naam}",
                "vervaldatum": vervaldatum,
                "risicoklasse": "laag",
                "toegestaneTools": list(tools),
                **extra,
            },
        )
        user = self.beheerder.maak("Users", body)
        return ProefAgent(str(user["id"]), naam, eigenaar_id, paar)

    def record(self, agent: ProefAgent) -> dict[str, Any]:
        """Het SCIM-record met effectieve status (beheerderstoken)."""
        return self.beheerder.haal("Users", agent.id)

    def ext(self, agent: ProefAgent) -> dict[str, Any]:
        """De agentextensie van het actuele record."""
        return dict(self.record(agent)[URN_AGENT])

    def bevestig(self, agent: ProefAgent, eigenaar_id: str | None = None) -> dict[str, Any]:
        """De eigenaar (of een andere eigenaar) bevestigt doel en huidige versie."""
        record = self.record(agent)
        return self.beheer.bevestig(
            agent.id,
            self.eigenaar_token(eigenaar_id or agent.eigenaar),
            record[URN_AGENT]["doel"],
            record["meta"]["version"],
        )

    def actieve_agent(self, team: str = TEAM_INKOMEN, **extra: Any) -> ProefAgent:
        """Medewerker in een team met een bevestigde agent."""
        agent = self.agent(self.mens(team), **extra)
        self.bevestig(agent)
        return agent

    # Poort ------------------------------------------------------------------------------------

    def toolcall(
        self,
        agent: ProefAgent,
        tool: str,
        dossier: str,
        *,
        assertie: str | None = None,
    ) -> Antwoord:
        """Rauwe MCP `tools/call` (stateless streamable HTTP) met een verse of gegeven assertie."""
        argumenten = {"dossier": dossier}
        if tool == "notitie_toevoegen":
            argumenten["tekst"] = f"integratietest {uuid.uuid4().hex[:6]}"
        token = assertie or agent.paar.assertie(agent.id, INST.poort_url)
        return rauw(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": argumenten},
            },
            f"Bearer {token}",
        )

    def notities(self, dossier: str) -> list[dict[str, Any]]:
        """Notities in de vakapplicatie (beheerderstoken op de poort)."""
        basis = INST.poort_url.rsplit("/", 1)[0]
        antwoord = httpx2.get(
            f"{basis}/dossiers/{dossier}/notities",
            headers={"Authorization": f"Bearer {self.beheerder_token}"},
            timeout=30,
        )
        antwoord.raise_for_status()
        return list(antwoord.json()["notities"])

    def toolrecords(self, agent: ProefAgent) -> list[dict[str, Any]]:
        """Toolcall-records van deze agent in het logboek."""
        return [
            r
            for r in self.beheer.logboek()
            if r["soort"] == "toolcall" and r.get("agent") == agent.id
        ]


def rauw(bericht: dict[str, Any] | bytes, authorization: str | None) -> Antwoord:
    """POST naar de poort; leest de weigeringscode uit `WWW-Authenticate`."""
    koppen = {"Accept": ACCEPT, "Content-Type": "application/json"}
    if authorization is not None:
        koppen["Authorization"] = authorization
    if isinstance(bericht, bytes):
        antwoord = httpx2.post(INST.poort_url, content=bericht, headers=koppen, timeout=30)
    else:
        antwoord = httpx2.post(INST.poort_url, json=bericht, headers=koppen, timeout=30)
    code = Weigering.uit_antwoord(antwoord).code if antwoord.status_code >= 400 else None
    return Antwoord(antwoord.status_code, code, antwoord.text)


def _zorg_team(bron: Scim, naam: str) -> str:
    gevonden = bron.zoek("Groups", f'displayName eq "{naam}"')
    if gevonden:
        return str(gevonden[0]["id"])
    return str(bron.maak("Groups", {"displayName": naam, "members": []})["id"])


@pytest.fixture(scope="session")
def keten() -> Keten:
    """De keten met de teams van de dossiers; maakt de teams aan als ze nog niet bestaan."""
    beheerder = lees_token(INST.geheimen_dir / "beheerder.token")
    bron = Scim(INST.scim_url, lees_token(INST.geheimen_dir / "bron.token"))
    try:
        teams = {t: _zorg_team(bron, t) for t in (TEAM_INKOMEN, TEAM_ZORG)}
    except BeheerFout as fout:
        pytest.fail(f"register niet bereikbaar of weigert de bron: {fout}")
    return Keten(
        bron, Scim(INST.scim_url, beheerder), Beheer(INST.register_url, beheerder), beheerder, teams
    )
