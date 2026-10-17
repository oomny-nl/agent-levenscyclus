"""Gedeelde hulpmiddelen voor de unittests.

Alleen de externe grenzen zijn vervangen: OpenFGA door `NepFga` (in-memory tuples onder de echte
synchronisatielogica van `Fga`) en HTTP naar het register door een `MockTransport`. Register,
motor, opslag, logboek en poort draaien echt, met SQLite en anker in `tmp_path`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from openfga_sdk import ReadRequestTupleKey

from levenscyclus.assertie import Sleutelpaar
from levenscyclus.beheer import agentresource
from levenscyclus.config import Instellingen
from levenscyclus.fga import Fga, FgaFout, FgaIds, Tuple
from levenscyclus.klok import Klok, iso
from levenscyclus.register.app import Dienst, maak_app
from levenscyclus.register.motor import Motor
from levenscyclus.register.opslag import Opslag
from levenscyclus.register.scim import URN_AGENT, URN_PATCH

MODEL = Path(__file__).resolve().parents[2] / "openfga" / "model.json"


class NepFga(Fga):
    """OpenFGA als in-memory tuples; `faal` maakt OpenFGA onbereikbaar."""

    def __init__(self) -> None:
        super().__init__("http://openfga.test", "sleutel", FgaIds("store", "model"))
        self.tuples: set[Tuple] = set()
        self.faal = False
        self.toegestaan = True
        self.checks: list[tuple[str, str, str]] = []

    def _grens(self) -> None:
        if self.faal:
            raise FgaFout("OpenFGA onbereikbaar (test)")

    async def _lees(self, sleutel: ReadRequestTupleKey) -> set[Tuple]:
        self._grens()
        obj = sleutel.object or ""
        return {
            t
            for t in self.tuples
            if (sleutel.user is None or t.user == sleutel.user)
            and (sleutel.relation is None or t.relation == sleutel.relation)
            and (t.object.startswith(obj) if obj.endswith(":") else t.object == obj)
        }

    async def _schrijf(self, toevoegen: set[Tuple], verwijderen: set[Tuple]) -> None:
        self._grens()
        self.tuples = (self.tuples - verwijderen) | toevoegen

    async def check(
        self, user: str, relation: str, obj: str, context: dict[str, Any] | None = None
    ) -> bool:
        self._grens()
        self.checks.append((user, relation, obj))
        return self.toegestaan


def instellingen(tmp_path: Path) -> Instellingen:
    """Instellingen met alle paden in `tmp_path`."""
    return Instellingen(
        register_url="http://register.test",
        poort_url="http://poort.test/mcp",
        openfga_url="http://openfga.test",
        openfga_store="test",
        geheimen_dir=tmp_path / "geheimen",
        db_pad=tmp_path / "register.db",
        anker_pad=tmp_path / "anker" / "logboek-anker.log",
    )


@dataclass
class Register:
    """Het echte register achter ASGI, met een nep-OpenFGA en tokens per rol."""

    dienst: Dienst
    fga: NepFga
    http: httpx2.AsyncClient
    tokens: dict[str, str]

    @property
    def motor(self) -> Motor:
        return self.dienst.m

    def koppen(self, rol: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[rol]}"}

    async def verzoek(
        self, methode: str, pad: str, rol: str, body: dict[str, Any] | None = None
    ) -> httpx2.Response:
        return await self.http.request(methode, pad, json=body, headers=self.koppen(rol))

    async def ok(
        self, methode: str, pad: str, rol: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        antwoord = await self.verzoek(methode, pad, rol, body)
        assert antwoord.status_code < 300, antwoord.text
        return dict(antwoord.json())

    async def patch(self, pad: str, rol: str, operaties: list[dict[str, Any]]) -> httpx2.Response:
        body = {"schemas": [URN_PATCH], "Operations": operaties}
        return await self.verzoek("PATCH", pad, rol, body)

    async def mens(self, naam: str) -> str:
        user = await self.ok("POST", "/scim/v2/Users", "bron", {"userName": naam})
        pad = self.dienst.inst.geheimen_dir / "eigenaren" / f"{user['id']}.token"
        self.tokens[user["id"]] = pad.read_text(encoding="utf-8").strip()
        return str(user["id"])

    async def team(self, naam: str, *leden: str) -> str:
        groep = await self.ok(
            "POST",
            "/scim/v2/Groups",
            "bron",
            {"displayName": naam, "members": [{"value": lid} for lid in leden]},
        )
        return str(groep["id"])

    async def agent(self, eigenaar: str, dagen: float = 30, **extra: Any) -> str:
        body = agentresource(
            f"agent-{len(self.tokens)}",
            eigenaar,
            Sleutelpaar.nieuw().publieke_jwk(),
            {
                "doel": "dossiers samenvatten",
                "vervaldatum": iso(self.dienst.klok.nu() + timedelta(days=dagen)),
                "risicoklasse": "laag",
                "toegestaneTools": ["dossier_lezen"],
                **extra,
            },
        )
        return str((await self.ok("POST", "/scim/v2/Users", "beheerder", body))["id"])

    async def record(self, id_: str) -> dict[str, Any]:
        return await self.ok("GET", f"/scim/v2/Users/{id_}", "beheerder")

    async def bevestig(self, id_: str) -> httpx2.Response:
        record = await self.record(id_)
        eigenaar = record[URN_AGENT]["eigenaar"]["value"]
        return await self.verzoek(
            "POST",
            f"/beheer/agents/{id_}/bevestiging",
            eigenaar,
            {"doel": record[URN_AGENT]["doel"], "versie": record["meta"]["version"]},
        )

    def opgeslagen(self, id_: str) -> dict[str, Any]:
        """Opgeslagen (niet effectief berekende) extensie."""
        resource = self.dienst.opslag.haal("User", id_)
        assert resource is not None
        return dict(resource[URN_AGENT])

    def gebeurtenissen(self, id_: str) -> list[str]:
        return [
            r["gebeurtenis"]
            for r in self.dienst.opslag.logboek()
            if r["soort"] == "levenscyclus" and r.get("agent") == id_
        ]


class NepDienst(Dienst):
    """Dienst waarvan `zorg_store` (HTTP naar OpenFGA) de id's van de nep-OpenFGA geeft."""

    async def _zorg_ids(self) -> FgaIds:
        return self.f.ids


def bouw_dienst(tmp_path: Path, fga: NepFga) -> Dienst:
    """Dienst zoals `Dienst.start` hem opzet, maar met de nep-OpenFGA en een demoklok."""
    inst = instellingen(tmp_path)
    opslag = Opslag(inst.db_pad, inst.anker_pad, inst.register_url)
    opslag.verifieer_bij_start()
    klok = Klok(demostand=True)
    model = json.loads(MODEL.read_text(encoding="utf-8"))
    dienst = NepDienst(inst, klok, opslag, "sleutel", model)
    dienst.fga = fga
    dienst.motor = Motor(opslag, fga, klok, inst)
    return dienst


@pytest.fixture
async def register(tmp_path: Path) -> AsyncIterator[Register]:
    fga = NepFga()
    dienst = bouw_dienst(tmp_path, fga)
    tokens = {
        rol: dienst.opslag.token_voor(rol, None, dienst.inst.geheimen_dir / f"{rol}.token")
        for rol in ("bron", "beheerder", "poort")
    }
    transport = httpx2.ASGITransport(app=maak_app(dienst))
    async with httpx2.AsyncClient(transport=transport, base_url="http://register.test") as http:
        yield Register(dienst, fga, http, tokens)
