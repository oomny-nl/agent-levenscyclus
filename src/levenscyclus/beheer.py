"""Clients voor het register: SCIM-provisioning (bron, beheerder) en de beheer-API.

De beheer-CLI en de demo gebruiken dezelfde functies (ONTWERP §4.7). Alles is synchroon: de
beheerder en HR zijn mensen met een terminal, geen agents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx2

from levenscyclus.logboek import Ketencontrole, controleer_tegen_anker, lees_anker
from levenscyclus.register.scim import MEDIA, URN_AGENT, URN_PATCH

TIMEOUT = 30


class BeheerFout(Exception):
    """Het register wees een verzoek af of is onbereikbaar.

    Attributes:
        status: HTTP-status (0 bij een verbindingsfout).
        detail: Tekst van het register.
    """

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}" if status else detail)
        self.status, self.detail = status, detail


def _controleer(antwoord: httpx2.Response) -> dict[str, Any]:
    if antwoord.status_code >= 400:
        try:
            body = antwoord.json()
        except ValueError:
            body = {}
        raise BeheerFout(antwoord.status_code, str(body.get("detail") or antwoord.text))
    if antwoord.status_code == 204 or not antwoord.content:
        return {}
    return dict(antwoord.json())


def lees_token(pad: Path) -> str:
    """Lees een token uit de geheimenmap.

    Raises:
        BeheerFout: Als het bestand ontbreekt (het register maakt het bij de eerste start).
    """
    try:
        return pad.read_text(encoding="utf-8").strip()
    except OSError as fout:
        raise BeheerFout(0, f"token {pad} niet leesbaar: {fout}") from fout


class Scim:
    """SCIM-client voor één principal (bron of beheerder).

    Args:
        scim_url: Basis van de SCIM-endpoints (`.../scim/v2`).
        token: Bearer-token van de principal.
    """

    def __init__(self, scim_url: str, token: str) -> None:
        self._http = httpx2.Client(
            base_url=scim_url,
            headers={"Authorization": f"Bearer {token}", "Accept": MEDIA},
            timeout=TIMEOUT,
        )

    def maak(self, soort: str, body: dict[str, Any]) -> dict[str, Any]:
        """`POST /Users` of `POST /Groups`."""
        return _controleer(self._http.post(f"/{soort}", json=body))

    def haal(self, soort: str, id_: str) -> dict[str, Any]:
        """`GET /{soort}/{id}`."""
        return _controleer(self._http.get(f"/{soort}/{id_}"))

    def alle(self, soort: str) -> list[dict[str, Any]]:
        """`GET /{soort}`; geeft alle resources (eerste pagina van 200)."""
        lijst = _controleer(self._http.get(f"/{soort}", params={"count": 200}))
        return list(lijst.get("Resources", []))

    def zoek(self, soort: str, filter_: str) -> list[dict[str, Any]]:
        """`GET /{soort}?filter=...`; geeft de resources."""
        lijst = _controleer(self._http.get(f"/{soort}", params={"filter": filter_}))
        return list(lijst.get("Resources", []))

    def patch(self, soort: str, id_: str, operaties: list[dict[str, Any]]) -> dict[str, Any]:
        """`PATCH /{soort}/{id}` met een PatchOp."""
        body = {"schemas": [URN_PATCH], "Operations": operaties}
        return _controleer(self._http.patch(f"/{soort}/{id_}", json=body))

    def vervang(self, soort: str, id_: str, body: dict[str, Any]) -> dict[str, Any]:
        """`PUT /{soort}/{id}`."""
        return _controleer(self._http.put(f"/{soort}/{id_}", json=body))

    def verwijder(self, soort: str, id_: str) -> None:
        """`DELETE /{soort}/{id}`."""
        _controleer(self._http.delete(f"/{soort}/{id_}"))


def agentresource(
    naam: str, eigenaar_id: str, publieke_jwk: str, extensie: dict[str, Any]
) -> dict[str, Any]:
    """Bouw het SCIM-record van een agent voor `POST /Users` door de beheerder.

    Args:
        naam: `userName` (ook `displayName`).
        eigenaar_id: SCIM-id van de menselijke eigenaar.
        publieke_jwk: Publieke sleutel als JWK-JSON.
        extensie: Overige extensieattributen (`doel`, `vervaldatum`, `risicoklasse`,
            `toegestaneTools`, optioneel termijnen).
    """
    return {
        "userName": naam,
        "displayName": naam,
        "userType": "agent",
        URN_AGENT: {
            "eigenaar": {"value": eigenaar_id},
            "publiekeSleutel": publieke_jwk,
            **extensie,
        },
    }


class Beheer:
    """Client voor de beheer-API met het beheerderstoken.

    Args:
        register_url: Basis-URL van het register.
        token: Beheerderstoken.
    """

    def __init__(self, register_url: str, token: str) -> None:
        self._basis = register_url
        self._http = httpx2.Client(
            base_url=register_url, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT
        )

    def overzicht(self) -> dict[str, Any]:
        """`GET /beheer/agents`: registertijd en één regel per agent."""
        return _controleer(self._http.get("/beheer/agents"))

    def agent(self, naam: str) -> dict[str, Any]:
        """De overzichtsregel van één agent op `userName`.

        Raises:
            BeheerFout: Als de agent niet bestaat.
        """
        for regel in self.overzicht()["agents"]:
            if regel["agent"] == naam:
                return regel
        raise BeheerFout(404, f"agent {naam!r} staat niet in het register")

    def controle(self) -> dict[str, Any]:
        """`POST /beheer/controle`: maakt statussen persistent en herbouwt de tuples."""
        return _controleer(self._http.post("/beheer/controle"))

    def relatiecheck(self, agent_id: str, dossier: str, relatie: str = "lezen") -> dict[str, Any]:
        """`GET /beheer/agents/{id}/relatiecheck`: OpenFGA rechtstreeks, zonder de poort."""
        return _controleer(
            self._http.get(
                f"/beheer/agents/{agent_id}/relatiecheck",
                params={"dossier": dossier, "relatie": relatie},
            )
        )

    def logboek(self) -> list[dict[str, Any]]:
        """`GET /logboek`: alle records."""
        return list(_controleer(self._http.get("/logboek"))["records"])

    def klok(self, vooruit: str) -> dict[str, Any]:
        """`POST /beheer/klok` (alleen met `DEMO_KLOK=1`)."""
        return _controleer(self._http.post("/beheer/klok", json={"vooruit": vooruit}))

    def openfga(self) -> dict[str, Any]:
        """`GET /openfga`: store- en model-id (herstelt ze zo nodig)."""
        return _controleer(self._http.get("/openfga"))

    def bevestig(
        self, agent_id: str, eigenaar_token: str, doel: str, versie: str
    ) -> dict[str, Any]:
        """`POST /beheer/agents/{id}/bevestiging` met het token van de eigenaar."""
        antwoord = httpx2.post(
            f"{self._basis}/beheer/agents/{agent_id}/bevestiging",
            headers={"Authorization": f"Bearer {eigenaar_token}"},
            json={"doel": doel, "versie": versie},
            timeout=TIMEOUT,
        )
        return _controleer(antwoord)


def controleer_logboek(records: list[dict[str, Any]], anker_pad: Path) -> Ketencontrole:
    """Controleer de keten uit `GET /logboek` tegen het ankerbestand."""
    return controleer_tegen_anker(records, lees_anker(anker_pad))
