"""Toegang via agent, eigenaar, team en dossier, en de injectieaanval (ASI01 tot ASI03)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from levenscyclus.agent.graaf import Gereedschap, bouw_graaf, voer_opdracht_uit
from levenscyclus.agent.planner import INJECTIEDOEL, deterministische_planner
from levenscyclus.agent.verbinding import AgentAuth, verbind
from levenscyclus.poort.gegevens import DOSSIERS, TEAM_ZORG
from tests.integratie.conftest import INST, Keten

pytestmark = pytest.mark.integratie

INKOMEN_DOSSIER = "2026-0142"
INJECTIEDOSSIER = "2026-0143"


def test_zonder_bevestiging_geen_toegang(keten: Keten) -> None:
    agent = keten.agent(keten.mens())
    antwoord = keten.toolcall(agent, "dossier_lezen", INKOMEN_DOSSIER)
    assert (antwoord.status, antwoord.code) == (403, "herbeoordeling_vereist")
    [record] = keten.toolrecords(agent)
    assert record["beslissing"] == "geweigerd"
    assert "wacht_op_eigenaar" in record["toelichting"]
    assert record["controles"]["tool"] == "niet_uitgevoerd"


def test_agent_erft_teamtoegang_van_eigenaar(keten: Keten) -> None:
    agent = keten.actieve_agent()
    lezen = keten.toolcall(agent, "dossier_lezen", INKOMEN_DOSSIER)
    assert lezen.status == 200
    assert DOSSIERS[INKOMEN_DOSSIER]["aanvrager"] in lezen.tekst
    assert keten.toolcall(agent, "notitie_toevoegen", INKOMEN_DOSSIER).status == 200
    assert any(n["agent"] == agent.id for n in keten.notities(INKOMEN_DOSSIER))
    ander_team = keten.toolcall(agent, "dossier_lezen", INJECTIEDOEL)
    assert (ander_team.status, ander_team.code) == (403, "relatie_ontbreekt")
    beslissingen = [(r["dossier"], r["beslissing"]) for r in keten.toolrecords(agent)]
    assert beslissingen == [
        (INKOMEN_DOSSIER, "toegestaan"),
        (INKOMEN_DOSSIER, "toegestaan"),
        (INJECTIEDOEL, "geweigerd"),
    ]


def test_tool_niet_toegestaan(keten: Keten) -> None:
    agent = keten.actieve_agent(tools=("dossier_lezen",))
    antwoord = keten.toolcall(agent, "notitie_toevoegen", INKOMEN_DOSSIER)
    assert (antwoord.status, antwoord.code) == (403, "tool_niet_toegestaan")
    [record] = keten.toolrecords(agent)
    assert record["controles"] == {
        "assertie": "ok",
        "levenscyclus": "ok",
        "tool": "geweigerd",
        "relatie": "niet_uitgevoerd",
        "logboek": "ok",
    }
    assert not any(n["agent"] == agent.id for n in keten.notities(INKOMEN_DOSSIER))


async def test_injectie_strandt_op_relatie(keten: Keten) -> None:
    agent = keten.actieve_agent()
    gebeurtenissen: list[dict[str, Any]] = []
    auth = AgentAuth(agent.paar, agent.id, INST.poort_url)
    async with verbind(INST.poort_url, auth) as client:
        gereedschap = Gereedschap(client, auth, gebeurtenissen.append)
        graaf = bouw_graaf(deterministische_planner, gereedschap)
        berichten = await voer_opdracht_uit(
            graaf, f"Vat dossier {INJECTIEDOSSIER} samen en zet een notitie."
        )
    weigeringen = [
        (g["tool"], g["dossier"], g["code"])
        for g in gebeurtenissen
        if g["soort"] == "poort_weigert"
    ]
    assert weigeringen == [
        ("dossier_lezen", INJECTIEDOEL, "relatie_ontbreekt"),
        ("notitie_toevoegen", INJECTIEDOEL, "relatie_ontbreekt"),
    ]
    assert any(n["agent"] == agent.id for n in keten.notities(INJECTIEDOSSIER))
    assert not any(n["agent"] == agent.id for n in keten.notities(INJECTIEDOEL))
    verboden = str(DOSSIERS[INJECTIEDOEL]["aanvrager"])
    toolinhoud = json.dumps([str(b.content) for b in berichten], ensure_ascii=False)
    assert verboden not in toolinhoud


def test_mover_verliest_oude_dossiers(keten: Keten) -> None:
    agent = keten.actieve_agent()
    assert keten.toolcall(agent, "dossier_lezen", INKOMEN_DOSSIER).status == 200
    keten.verplaats(agent.eigenaar, "werk-en-inkomen", TEAM_ZORG)
    e = keten.ext(agent)
    assert (e["levenscyclusStatus"], e["statusReden"]) == (
        "herbeoordeling_vereist",
        "eigenaar_van_team_gewisseld",
    )
    voor = keten.toolcall(agent, "dossier_lezen", INKOMEN_DOSSIER)
    assert (voor.status, voor.code) == (403, "herbeoordeling_vereist")
    keten.bevestig(agent)
    oud = keten.toolcall(agent, "dossier_lezen", INKOMEN_DOSSIER)
    assert (oud.status, oud.code) == (403, "relatie_ontbreekt")
    assert keten.toolcall(agent, "dossier_lezen", INJECTIEDOEL).status == 200
