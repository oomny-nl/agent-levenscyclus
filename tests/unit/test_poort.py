"""De poort faalt dicht (ASI08): zonder register, OpenFGA of logboek geen tooluitvoering.

De echte poort (middleware, MCP-server, controles, SQLite) draait via Starlettes TestClient; het
register is een `MockTransport` en OpenFGA de `NepFga`.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import jwt
import pytest
from starlette.testclient import TestClient

from levenscyclus.assertie import Sleutelpaar
from levenscyclus.fga import Tuple
from levenscyclus.poort.app import maak_app
from levenscyclus.poort.opslag import PoortOpslag
from levenscyclus.poort.register import Register
from levenscyclus.poort.toegang import STAPPEN, Toegang, _record
from levenscyclus.register import app as register_app
from levenscyclus.register.app import maak_app as maak_registerapp
from levenscyclus.register.scim import URN_AGENT
from tests.unit.conftest import NepFga, instellingen
from tests.unit.conftest import Register as Registerproef

# Een id zoals het register hem uitgeeft (`str(uuid.uuid4())`).
AGENT = "3f2b8c1e-5d4a-4e6f-9a7b-1c2d3e4f5a6b"
DOSSIER = "2026-0142"
AUD = "http://poort.test/mcp"


@dataclass
class NepRegister:
    """Het register zoals de poort het via HTTP ziet.

    `onbereikbaar` laat het lezen van records falen, `logboek` stuurt het antwoord op het
    logboekrecord, `teams` is de SCIM-id per groepsnaam.
    """

    paar: Sleutelpaar
    onbereikbaar: bool = False
    logboek: str = "201"
    ander_id: bool = False
    gelogd: list[dict[str, Any]] = field(default_factory=list)
    paden: list[str] = field(default_factory=list)
    teams: dict[str, str] = field(default_factory=lambda: {"werk-en-inkomen": "t1", "zorg": "t2"})

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
        self.paden.append(request.url.raw_path.decode())
        if self.onbereikbaar and pad.startswith("/scim/"):
            raise httpx2.ConnectError("register weg (test)", request=request)
        if pad == f"/scim/v2/Users/{AGENT}":
            record = self.agentrecord()
            if self.ander_id:
                record["id"] = "0e0e0e0e-0000-4000-8000-000000000000"
            return httpx2.Response(200, json=record)
        if pad == "/scim/v2/Groups":
            naam = json.loads(request.url.params["filter"].removeprefix("displayName eq "))
            teams = [{"id": self.teams[naam], "displayName": naam}] if naam in self.teams else []
            return httpx2.Response(200, json={"totalResults": len(teams), "Resources": teams})
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
            controles = {**record["controles"], "logboek": "geweigerd"}
            afgewezen = {
                **record,
                "beslissing": "geweigerd",
                "reden": "toestand_gewijzigd",
                "toelichting": "recordversie verouderd",
                "controles": controles,
            }
            return httpx2.Response(409, json=afgewezen)
        return httpx2.Response(201, json={"nr": len(self.gelogd), **record})


@dataclass
class Proef:
    client: TestClient
    register: NepRegister
    fga: NepFga
    opslag: PoortOpslag
    paar: Sleutelpaar

    def notitie(self, assertie: str | None = None) -> httpx2.Response:
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
            "Authorization": f"Bearer {assertie or self.paar.assertie(AGENT, AUD)}",
            "Accept": "application/json, text/event-stream",
        }
        return self.client.post("/mcp", json=bericht, headers=koppen)


def _toegang(tmp_path: Path, paar: Sleutelpaar) -> tuple[Toegang, NepRegister, NepFga]:
    nep = NepRegister(paar)
    register = Register("http://register.test", "poort-token")
    register._http = httpx2.AsyncClient(
        transport=httpx2.MockTransport(nep), base_url="http://register.test"
    )
    fga = NepFga()
    opslag = PoortOpslag(tmp_path / "poort.db")
    return Toegang(instellingen(tmp_path), register, fga, opslag), nep, fga


@pytest.fixture
def proef(tmp_path: Path) -> Iterator[Proef]:
    paar = Sleutelpaar.nieuw()
    toegang, nep, fga = _toegang(tmp_path, paar)
    app = maak_app(toegang, toegang.opslag, toegang.register)
    with TestClient(app, base_url="http://poort.test") as client:
        yield Proef(client, nep, fga, toegang.opslag, paar)


async def test_gelijktijdige_toolcalls_koppelen_het_dossier_een_keer(tmp_path: Path) -> None:
    # Na een herstart of teamwissel kent de poort de koppeling nog niet; twee toolcalls tegelijk
    # mogen dan niet allebei `dossier#team` schrijven (de tweede write zou falen).
    paar = Sleutelpaar.nieuw()
    toegang, nep, fga = _toegang(tmp_path, paar)
    besluiten, _ = await asyncio.gather(
        asyncio.gather(
            *(
                toegang.controleer(f"Bearer {paar.assertie(AGENT, AUD)}", "dossier_lezen", DOSSIER)
                for _ in range(3)
            )
        ),
        toegang.herstel(),
    )
    assert [b.record["beslissing"] for b in besluiten] == ["toegestaan"] * 3
    assert Tuple("team:t1", "team", f"dossier:{DOSSIER}") in fga.tuples
    # Alleen het expliciete herstel vraagt `/openfga`: geen mislukte write die een tweede
    # herstel en een herhaalde check afdwingt.
    assert nep.paden.count("/openfga") == 1


def _code(antwoord: Any) -> str:
    return str(antwoord.headers.get("www-authenticate", ""))


def test_toegestane_toolcall_voert_uit(proef: Proef) -> None:
    antwoord = proef.notitie()
    assert antwoord.status_code == 200, antwoord.text
    assert len(proef.opslag.notities(DOSSIER)) == 1
    assert proef.register.gelogd[-1]["beslissing"] == "toegestaan"


def test_vervangen_team_krijgt_de_dossierrelatie(proef: Proef) -> None:
    def dossierrelatie() -> set[Tuple]:
        return {t for t in proef.fga.tuples if t.object == f"dossier:{DOSSIER}"}

    assert proef.notitie().status_code == 200
    assert dossierrelatie() == {Tuple("team:t1", "team", f"dossier:{DOSSIER}")}
    # Team verwijderd en opnieuw aangemaakt: een nieuwe SCIM-id, de relatie verhuist mee.
    proef.register.teams["werk-en-inkomen"] = "t9"
    assert proef.notitie().status_code == 200
    assert dossierrelatie() == {Tuple("team:t9", "team", f"dossier:{DOSSIER}")}
    # Team weg: het dossier houdt geen relatie met een team dat niet meer bestaat.
    del proef.register.teams["werk-en-inkomen"]
    proef.notitie()
    assert dossierrelatie() == set()


async def test_team_buiten_de_eerste_groepenpagina_gevonden(register: Registerproef) -> None:
    for i in range(120):
        await register.team(f"team-{i:03}")
    gezocht = await register.team("Werk-en-Inkomen")
    client = Register("http://register.test", register.tokens["poort"])
    client._http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=maak_registerapp(register.dienst)),
        base_url="http://register.test",
        headers={"Authorization": f"Bearer {register.tokens['poort']}"},
    )
    # De standaardpagina telt honderd groepen; de gezochte is de 121e.
    assert await client.team_id("werk-en-inkomen") == gezocht
    assert await client.team_id("bestaat-niet") is None


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
    assert "recordversie verouderd" in _code(antwoord)
    assert proef.opslag.notities(DOSSIER) == []


def test_geweigerde_toolcall_staat_vastgelegd_met_logboek_ok(proef: Proef) -> None:
    # Stap 5 legt ook een eerdere weigering vast; het record zegt dus niet `niet_uitgevoerd`.
    proef.fga.toegestaan = False
    assert proef.notitie().status_code == 403
    [record] = proef.register.gelogd
    assert record["controles"] == {
        "assertie": "ok",
        "levenscyclus": "ok",
        "tool": "geweigerd",
        "relatie": "niet_uitgevoerd",
        "logboek": "ok",
    }


def test_toolcall_record_volgt_het_registerschema() -> None:
    controles = {**dict.fromkeys(STAPPEN, "ok"), "logboek": "niet_uitgevoerd"}
    record = _record(None, "dossier_lezen", None, controles, None)
    assert record["controles"]["logboek"] == "ok"
    assert set(record) == set(register_app.TOOLCALL_VELDEN)
    assert STAPPEN == register_app.STAPPEN
    assert register_app._valideer_toolcall(record) == record


def _assertie(proef: Proef, sub: str = AGENT, **claims: Any) -> str:
    nu = int(time.time())
    basis = {"iss": sub, "sub": sub, "aud": AUD, "iat": nu, "exp": nu + 30, "jti": "j" * 24}
    return jwt.encode(
        {**basis, **claims}, proef.paar.prive, algorithm="ES256", headers={"typ": "JWT"}
    )


@pytest.mark.parametrize(
    "claims",
    [{"iat": f"{int(time.time())}"}, {"exp": 10**400}, {"iat": -(10**400)}],
    ids=["string", "exp-buiten-float", "iat-buiten-float"],
)
def test_ongeldige_tijdclaim_wordt_gelogde_weigering(proef: Proef, claims: dict[str, Any]) -> None:
    antwoord = proef.notitie(_assertie(proef, **claims))
    assert antwoord.status_code == 401
    assert 'error="assertie_ongeldig"' in _code(antwoord)
    record = proef.register.gelogd[-1]
    assert (record["reden"], record["controles"]["assertie"]) == ("assertie_ongeldig", "geweigerd")
    assert proef.opslag.notities(DOSSIER) == []


@pytest.mark.parametrize(
    "sub", ["../../../openfga", "x?filter=1", "x/../y", AGENT.upper(), "{" + AGENT + "}", "agent-1"]
)
def test_sub_buiten_de_idvorm_bereikt_het_register_niet(proef: Proef, sub: str) -> None:
    proef.register.paden.clear()
    antwoord = proef.notitie(_assertie(proef, sub))
    assert antwoord.status_code == 401
    assert 'error="agent_onbekend"' in _code(antwoord)
    assert proef.register.paden == ["/logboek"]
    assert proef.opslag.notities(DOSSIER) == []


def test_register_met_ander_record_faalt_dicht(proef: Proef) -> None:
    proef.register.ander_id = True
    antwoord = proef.notitie()
    assert antwoord.status_code == 503
    assert proef.opslag.notities(DOSSIER) == []
    assert proef.register.gelogd[-1]["controles"]["assertie"] == "geweigerd"


def test_jti_overleeft_herstart(tmp_path: Path) -> None:
    pad = tmp_path / "poort.db"
    assert PoortOpslag(pad).reserveer_jti(AGENT, "j" * 24, 2_000_000_000)
    na_herstart = PoortOpslag(pad)
    assert not na_herstart.reserveer_jti(AGENT, "j" * 24, 2_000_000_000)
    assert na_herstart.reserveer_jti(AGENT, "k" * 24, 2_000_000_000)
