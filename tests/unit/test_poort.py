"""De poort faalt dicht (ASI08): zonder register, OpenFGA of logboek geen tooluitvoering.

De echte poort (middleware, MCP-server, controles, SQLite) draait via Starlettes TestClient; het
register is een `MockTransport` en OpenFGA de `NepFga`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import pytest
from starlette.testclient import TestClient

from levenscyclus.assertie import Sleutelpaar
from levenscyclus.poort.app import maak_app
from levenscyclus.poort.opslag import PoortOpslag
from levenscyclus.poort.register import Register
from levenscyclus.poort.toegang import Toegang
from levenscyclus.register.scim import URN_AGENT
from tests.unit.conftest import NepFga, instellingen

AGENT = "agent-1"
DOSSIER = "2026-0142"


@dataclass
class NepRegister:
    """Het register zoals de poort het via HTTP ziet.

    `onbereikbaar` laat het lezen van records falen, `logboek` stuurt het antwoord op het
    logboekrecord.
    """

    paar: Sleutelpaar
    onbereikbaar: bool = False
    logboek: str = "201"
    gelogd: list[dict[str, Any]] = field(default_factory=list)

    def agentrecord(self) -> dict[str, Any]:
        return {
            "id": AGENT,
            "userName": "dossierassistent",
            "userType": "agent",
            "active": True,
            "meta": {"version": 'W/"3"'},
            URN_AGENT: {
                "eigenaar": {"value": "m1", "display": "Sanne"},
                "publiekeSleutel": self.paar.publieke_jwk(),
                "levenscyclusStatus": "actief",
                "statusReden": "bevestigd_door_eigenaar",
                "beoordeeldOp": "2026-10-17T12:00:00Z",
            },
        }

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        pad = request.url.path
        if self.onbereikbaar and pad.startswith("/scim/"):
            raise httpx2.ConnectError("register weg (test)", request=request)
        if pad == f"/scim/v2/Users/{AGENT}":
            return httpx2.Response(200, json=self.agentrecord())
        if pad == "/scim/v2/Groups":
            teams = [
                {"id": "t1", "displayName": "werk-en-inkomen"},
                {"id": "t2", "displayName": "zorg"},
            ]
            return httpx2.Response(200, json={"Resources": teams})
        if pad == "/openfga":
            return httpx2.Response(200, json={"store_id": "store", "model_id": "model"})
        if pad == "/logboek" and request.method == "POST":
            return self._log(json.loads(request.content))
        return httpx2.Response(404)

    def _log(self, record: dict[str, Any]) -> httpx2.Response:
        self.gelogd.append(record)
        if self.logboek == "weg":
            raise httpx2.ConnectError("logboek weg (test)")
        if self.logboek == "503":
            return httpx2.Response(503, json={"detail": "ankerwrite mislukt"})
        if self.logboek == "409":
            reden = "toestand_gewijzigd: recordversie verouderd"
            return httpx2.Response(409, json={**record, "beslissing": "geweigerd", "reden": reden})
        return httpx2.Response(201, json={"nr": len(self.gelogd), **record})


@dataclass
class Proef:
    client: TestClient
    register: NepRegister
    fga: NepFga
    opslag: PoortOpslag
    paar: Sleutelpaar

    def notitie(self) -> httpx2.Response:
        bericht = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "notitie_toevoegen",
                "arguments": {"dossier": DOSSIER, "tekst": "concept"},
            },
        }
        koppen = {
            "Authorization": f"Bearer {self.paar.assertie(AGENT, 'http://poort.test/mcp')}",
            "Accept": "application/json, text/event-stream",
        }
        return self.client.post("/mcp", json=bericht, headers=koppen)


@pytest.fixture
def proef(tmp_path: Path) -> Iterator[Proef]:
    paar = Sleutelpaar.nieuw()
    nep = NepRegister(paar)
    register = Register("http://register.test", "poort-token")
    register._http = httpx2.AsyncClient(
        transport=httpx2.MockTransport(nep), base_url="http://register.test"
    )
    fga = NepFga()
    opslag = PoortOpslag(tmp_path / "poort.db")
    toegang = Toegang(instellingen(tmp_path), register, fga, opslag)
    with TestClient(maak_app(toegang, opslag, register), base_url="http://poort.test") as client:
        yield Proef(client, nep, fga, opslag, paar)


def _code(antwoord: Any) -> str:
    return str(antwoord.headers.get("www-authenticate", ""))


def test_toegestane_toolcall_voert_uit(proef: Proef) -> None:
    antwoord = proef.notitie()
    assert antwoord.status_code == 200, antwoord.text
    assert len(proef.opslag.notities(DOSSIER)) == 1
    assert proef.register.gelogd[-1]["beslissing"] == "toegestaan"


def test_register_onbereikbaar_faalt_dicht(proef: Proef) -> None:
    proef.register.onbereikbaar = True
    antwoord = proef.notitie()
    assert antwoord.status_code == 503
    assert 'error="register_onbereikbaar"' in _code(antwoord)
    assert proef.opslag.notities(DOSSIER) == []
    assert proef.register.gelogd[-1]["controles"]["assertie"] == "geweigerd"
    # Is ook het logboek weg, dan blijft de poort dicht met audit_niet_vastgelegd.
    proef.register.logboek = "weg"
    assert proef.notitie().status_code == 503
    assert proef.opslag.notities(DOSSIER) == []


def test_openfga_onbereikbaar_faalt_dicht(proef: Proef) -> None:
    proef.fga.faal = True
    antwoord = proef.notitie()
    assert antwoord.status_code == 503
    assert 'error="openfga_onbereikbaar"' in _code(antwoord)
    assert proef.opslag.notities(DOSSIER) == []
    record = proef.register.gelogd[-1]
    assert (record["beslissing"], record["controles"]["tool"]) == ("geweigerd", "geweigerd")


@pytest.mark.parametrize("storing", ["503", "weg"])
def test_logboek_onbereikbaar_faalt_dicht(proef: Proef, storing: str) -> None:
    proef.register.logboek = storing
    antwoord = proef.notitie()
    assert antwoord.status_code == 503
    assert 'error="audit_niet_vastgelegd"' in _code(antwoord)
    assert proef.opslag.notities(DOSSIER) == []


def test_toestand_gewijzigd_voert_tool_niet_uit(proef: Proef) -> None:
    proef.register.logboek = "409"
    antwoord = proef.notitie()
    assert antwoord.status_code == 403
    assert 'error="toestand_gewijzigd"' in _code(antwoord)
    assert proef.opslag.notities(DOSSIER) == []


def test_jti_overleeft_herstart(tmp_path: Path) -> None:
    pad = tmp_path / "poort.db"
    assert PoortOpslag(pad).reserveer_jti(AGENT, "j" * 24, 2_000_000_000)
    na_herstart = PoortOpslag(pad)
    assert not na_herstart.reserveer_jti(AGENT, "j" * 24, 2_000_000_000)
    assert na_herstart.reserveer_jti(AGENT, "k" * 24, 2_000_000_000)
