"""Levenscyclus tegen de echte keten: leaver, noodstop, verval, herbeoordeling, wijzigingen.

Tijdsprongen zijn klein (uren), zodat ze de agents van andere tests niet over hun termijnen duwen.
"""

from __future__ import annotations

import os
import uuid

import httpx2
import pytest
from mcp.shared.exceptions import MCPError

from levenscyclus.agent.verbinding import AgentAuth, verbind
from levenscyclus.assertie import Sleutelpaar
from levenscyclus.beheer import lees_token
from levenscyclus.fga import Fga, FgaIds, Tuple
from levenscyclus.klok import parse_tijd
from levenscyclus.poort.gegevens import TEAM_INKOMEN
from levenscyclus.register.scim import URN_AGENT
from tests.integratie.conftest import INST, Keten, ProefAgent

pytestmark = pytest.mark.integratie

DOSSIER = "2026-0142"


def _fga(keten: Keten) -> Fga:
    """OpenFGA met de id's die het register teruggeeft (tests hebben de preshared key)."""
    ids = keten.beheer.openfga()
    return Fga(
        INST.openfga_url,
        os.environ["OPENFGA_PRESHARED_KEY"],
        FgaIds(ids["store_id"], ids["model_id"]),
    )


async def _toegang(keten: Keten, agent: ProefAgent) -> set[Tuple]:
    fga = _fga(keten)
    subject = f"agent:{agent.id}"
    return await fga.lees_per_subject(subject, "gebruiker") | await fga.lees_per_subject(
        subject, "tool"
    )


def _patch_agent(keten: Keten, agent: ProefAgent, operaties: list[dict[str, object]]) -> None:
    keten.beheerder.patch("Users", agent.id, operaties)


async def test_leaver_tijdens_opdracht(keten: Keten) -> None:
    agent = keten.actieve_agent()
    auth = AgentAuth(agent.paar, agent.id, INST.poort_url)
    async with verbind(INST.poort_url, auth) as client:
        eerste = await client.call_tool("dossier_lezen", {"dossier": DOSSIER})
        assert not eerste.is_error
        keten.vertrek(agent.eigenaar)
        with pytest.raises(MCPError):
            await client.call_tool("notitie_toevoegen", {"dossier": DOSSIER, "tekst": "x"})
    assert auth.laatste_weigering is not None
    assert auth.laatste_weigering.code == "agent_gedeactiveerd"
    assert "eigenaar_vertrokken" in auth.laatste_weigering.reden
    assert not any(n["agent"] == agent.id for n in keten.notities(DOSSIER))
    laatste = keten.toolrecords(agent)[-1]
    assert (laatste["tool"], laatste["beslissing"]) == ("notitie_toevoegen", "geweigerd")


async def test_leaver_deactiveert_agent(keten: Keten) -> None:
    agent = keten.actieve_agent()
    assert await _toegang(keten, agent)
    keten.vertrek(agent.eigenaar)
    record = keten.record(agent)
    e = record[URN_AGENT]
    assert record["active"] is False
    assert (e["levenscyclusStatus"], e["statusReden"]) == ("gedeactiveerd", "eigenaar_vertrokken")
    assert await _toegang(keten, agent) == set()
    regel = keten.beheer.agent(agent.naam)
    assert (regel["status"], regel["reden"]) == ("gedeactiveerd", "eigenaar_vertrokken")
    antwoord = keten.toolcall(agent, "dossier_lezen", DOSSIER)
    assert (antwoord.status, antwoord.code) == (403, "agent_gedeactiveerd")


def test_noodstop(keten: Keten) -> None:
    agent = keten.actieve_agent()
    _patch_agent(keten, agent, [{"op": "replace", "path": "active", "value": False}])
    e = keten.ext(agent)
    assert (e["levenscyclusStatus"], e["statusReden"]) == (
        "gedeactiveerd",
        "uitgeschakeld_door_beheerder",
    )
    antwoord = keten.toolcall(agent, "dossier_lezen", DOSSIER)
    assert (antwoord.status, antwoord.code) == (403, "agent_gedeactiveerd")
    _patch_agent(keten, agent, [{"op": "replace", "path": "active", "value": True}])
    e = keten.ext(agent)
    assert (e["levenscyclusStatus"], e["statusReden"]) == (
        "herbeoordeling_vereist",
        "heractiveerd_door_beheerder",
    )
    assert keten.toolcall(agent, "dossier_lezen", DOSSIER).code == "herbeoordeling_vereist"
    keten.bevestig(agent)
    assert keten.toolcall(agent, "dossier_lezen", DOSSIER).status == 200


def test_verlopen_voor_controle(keten: Keten) -> None:
    agent = keten.actieve_agent(vervaldagen=1 / 24)
    assert keten.toolcall(agent, "dossier_lezen", DOSSIER).status == 200
    keten.klok("PT2H")
    antwoord = keten.toolcall(agent, "dossier_lezen", DOSSIER)
    assert (antwoord.status, antwoord.code) == (403, "agent_verlopen")
    e = keten.ext(agent)
    assert (e["levenscyclusStatus"], e["statusReden"]) == ("verlopen", "vervaldatum_bereikt")
    assert parse_tijd(e["statusSinds"]) == parse_tijd(e["vervaldatum"])
    overgangen = {o["agent"]: o for o in keten.beheer.controle()["overgangen"]}
    assert overgangen[agent.id]["naar"] == "verlopen"
    assert parse_tijd(overgangen[agent.id]["sinds"]) == parse_tijd(e["vervaldatum"])


def test_openfga_weigert_verlopen_los_van_poort(keten: Keten) -> None:
    agent = keten.actieve_agent(vervaldagen=1 / 24)
    assert keten.beheer.relatiecheck(agent.id, DOSSIER)["allowed"] is True
    keten.klok("PT2H")
    # Geen controle gedraaid: het tuple gebruiker#agent bestaat nog, de conditie weigert.
    assert keten.beheer.relatiecheck(agent.id, DOSSIER)["allowed"] is False


async def test_controle_ruimt_verweesde_register_tuples_op(keten: Keten) -> None:
    # Tuples van een agent en een medewerker die niet in het register staan, zoals na een DELETE
    # waarvan de opruiming mislukte. De dossierrelaties van de poort blijven staan.
    fga = _fga(keten)
    agent, mens = f"agent:{uuid.uuid4()}", f"gebruiker:{uuid.uuid4()}"
    team = f"team:{keten.teams[TEAM_INKOMEN]}"
    await fga.synchroniseer_subject(
        agent, "tool", {Tuple(agent, "toegestaan", "tool:dossier_lezen")}
    )
    await fga.synchroniseer_subject(mens, "team", {Tuple(mens, "lid", team)})
    await fga.synchroniseer(agent, "eigenaar", {Tuple(mens, "eigenaar", agent)})
    dossiers = await fga.lees(f"dossier:{DOSSIER}", "team")
    assert dossiers
    keten.beheer.controle()
    assert await fga.lees_per_subject(agent, "tool") == set()
    assert await fga.lees_per_subject(mens, "team") == set()
    assert await fga.lees(agent) == set()
    assert await fga.lees(f"dossier:{DOSSIER}", "team") == dossiers


def test_niet_herbeoordeeld_wordt_gedeactiveerd(keten: Keten) -> None:
    agent = keten.actieve_agent(herbeoordelingTermijn="PT1H", bevestigingstermijn="PT1H")
    volgende = parse_tijd(keten.ext(agent)["volgendeHerbeoordeling"])
    keten.klok("PT61M")
    e = keten.ext(agent)
    assert (e["levenscyclusStatus"], e["statusReden"]) == (
        "herbeoordeling_vereist",
        "periodieke_herbeoordeling",
    )
    assert parse_tijd(e["statusSinds"]) == volgende
    assert keten.toolcall(agent, "dossier_lezen", DOSSIER).code == "herbeoordeling_vereist"
    keten.klok("PT1H")
    e = keten.ext(agent)
    assert (e["levenscyclusStatus"], e["statusReden"]) == ("gedeactiveerd", "niet_herbeoordeeld")
    assert keten.toolcall(agent, "dossier_lezen", DOSSIER).code == "agent_gedeactiveerd"
    keten.beheer.controle()
    assert keten.record(agent)["active"] is False


def test_configuratiewijziging_vereist_herbeoordeling(keten: Keten) -> None:
    agent = keten.actieve_agent()
    nieuw = Sleutelpaar.nieuw()
    _patch_agent(
        keten,
        agent,
        [{"op": "replace", "path": f"{URN_AGENT}:publiekeSleutel", "value": nieuw.publieke_jwk()}],
    )
    e = keten.ext(agent)
    assert (e["levenscyclusStatus"], e["statusReden"]) == (
        "herbeoordeling_vereist",
        "configuratie_gewijzigd",
    )
    oude_sleutel = keten.toolcall(agent, "dossier_lezen", DOSSIER)
    assert (oude_sleutel.status, oude_sleutel.code) == (401, "assertie_ongeldig")
    met_nieuw = ProefAgent(agent.id, agent.naam, agent.eigenaar, nieuw)
    nieuwe_sleutel = keten.toolcall(met_nieuw, "dossier_lezen", DOSSIER)
    assert (nieuwe_sleutel.status, nieuwe_sleutel.code) == (403, "herbeoordeling_vereist")
    keten.bevestig(agent)
    assert keten.toolcall(met_nieuw, "dossier_lezen", DOSSIER).status == 200


async def test_toolintrekking_verwijdert_tuple(keten: Keten) -> None:
    agent = keten.actieve_agent()
    tools = {t.object for t in await _toegang(keten, agent) if t.relation == "toegestaan"}
    assert tools == {"tool:dossier_lezen", "tool:notitie_toevoegen"}
    _patch_agent(
        keten,
        agent,
        [{"op": "replace", "path": f"{URN_AGENT}:toegestaneTools", "value": ["dossier_lezen"]}],
    )
    keten.bevestig(agent)
    tools = {t.object for t in await _toegang(keten, agent) if t.relation == "toegestaan"}
    assert tools == {"tool:dossier_lezen"}
    antwoord = keten.toolcall(agent, "notitie_toevoegen", DOSSIER)
    assert antwoord.code == "tool_niet_toegestaan"


async def test_mislukte_eigenaarswissel_laat_geen_oude_relatie(keten: Keten) -> None:
    agent = keten.actieve_agent()
    oud = agent.eigenaar
    nieuw = keten.mens()
    _patch_agent(
        keten,
        agent,
        [{"op": "replace", "path": f"{URN_AGENT}:eigenaar", "value": {"value": nieuw}}],
    )
    e = keten.ext(agent)
    assert (e["levenscyclusStatus"], e["statusReden"]) == (
        "herbeoordeling_vereist",
        "nieuwe_eigenaar",
    )
    # Nabootsen van een mislukte synchronisatie: de delegatielijn naar de oude eigenaar staat er
    # nog. De bevestiging leest per subject en ruimt hem op.
    fga = _fga(keten)
    achtergebleven = Tuple(f"agent:{agent.id}", "agent", f"gebruiker:{oud}", e["vervaldatum"])
    await fga.synchroniseer_subject(f"agent:{agent.id}", "gebruiker", {achtergebleven})
    keten.bevestig(agent, nieuw)
    delegatie = await fga.lees_per_subject(f"agent:{agent.id}", "gebruiker")
    assert {t.object for t in delegatie} == {f"gebruiker:{nieuw}"}
    eigenaar = await fga.lees(f"agent:{agent.id}", "eigenaar")
    assert {t.user for t in eigenaar} == {f"gebruiker:{nieuw}"}


def test_toelating_na_tussentijdse_mover_geweigerd(keten: Keten) -> None:
    agent = keten.actieve_agent()
    poort = httpx2.Client(
        base_url=INST.register_url,
        headers={"Authorization": f"Bearer {lees_token(INST.geheimen_dir / 'poort.token')}"},
        timeout=30,
    )
    # Stap 1 en 2 van de poort: actief record, versie v.
    gelezen = poort.get(f"/scim/v2/Users/{agent.id}").json()
    assert gelezen[URN_AGENT]["levenscyclusStatus"] == "actief"
    # Tussen lezen en logboekrecord: mover en een nieuwe bevestiging.
    keten.verplaats(agent.eigenaar, "werk-en-inkomen", "zorg")
    keten.bevestig(agent)
    antwoord = poort.post(
        "/logboek",
        json={
            "soort": "toolcall",
            "agent": agent.id,
            "agent_naam": agent.naam,
            "tool": "dossier_lezen",
            "dossier": "2026-0301",
            "beslissing": "toegestaan",
            "controles": dict.fromkeys(
                ("assertie", "levenscyclus", "tool", "relatie", "logboek"), "ok"
            ),
            "recordversie": gelezen["meta"]["version"],
            "jti": "integratietest-tussentijds",
        },
    )
    assert antwoord.status_code == 409
    record = antwoord.json()
    assert record["beslissing"] == "geweigerd"
    assert record["reden"].startswith("toestand_gewijzigd")
