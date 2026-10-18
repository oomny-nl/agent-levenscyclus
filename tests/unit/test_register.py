"""Register en motor met nep-OpenFGA: fail closed, tijdregels, noodstop en teams."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from levenscyclus.fga import Tuple
from levenscyclus.klok import iso, parse_tijd
from levenscyclus.register.motor import effectief
from levenscyclus.register.scim import AGENT_CONFIGURATIE, URN_AGENT
from tests.unit.conftest import Register


async def _actieve_agent(register: Register, **extra: Any) -> tuple[str, str, str]:
    """(eigenaar, team, agent) met een bevestigde agent."""
    eigenaar = await register.mens(f"mw-{len(register.tokens)}")
    team = await register.team(f"team-{len(register.tokens)}", eigenaar)
    agent = await register.agent(eigenaar, **extra)
    assert (await register.bevestig(agent)).status_code == 200
    return eigenaar, team, agent


def _ext(record: dict[str, object]) -> dict[str, object]:
    waarde = record[URN_AGENT]
    assert isinstance(waarde, dict)
    return waarde


async def test_bevestiging_zonder_sync_activeert_niet(register: Register) -> None:
    eigenaar = await register.mens("sanne")
    await register.team("werk", eigenaar)
    agent = await register.agent(eigenaar)
    register.fga.faal = True
    antwoord = await register.bevestig(agent)
    assert antwoord.status_code == 503
    assert register.opgeslagen(agent)["levenscyclusStatus"] == "herbeoordeling_vereist"
    assert "tuples_niet_gesynchroniseerd" in register.gebeurtenissen(agent)
    register.fga.faal = False
    assert not any(t.user == f"agent:{agent}" for t in register.fga.tuples)


async def test_mover_blokkeert_ook_zonder_openfga(register: Register) -> None:
    eigenaar, team, agent = await _actieve_agent(register)
    register.fga.faal = True
    verwijder = [{"op": "remove", "path": f'members[value eq "{eigenaar}"]'}]
    await register.patch(f"/scim/v2/Groups/{team}", "bron", verwijder)
    # De blokkering staat in dezelfde transactie als de groepsmutatie, vóór de synchronisatie.
    e = register.opgeslagen(agent)
    assert (e["levenscyclusStatus"], e["statusReden"]) == (
        "herbeoordeling_vereist",
        "eigenaar_van_team_gewisseld",
    )
    groep = register.dienst.opslag.haal("Group", team)
    assert groep is not None
    assert groep["members"] == []


async def test_controle_zet_verlopen(register: Register) -> None:
    _, _, agent = await _actieve_agent(register, dagen=1)
    assert any(t.user == f"agent:{agent}" for t in register.fga.tuples)
    register.dienst.klok.zet_vooruit(timedelta(days=2))
    vervaldatum = parse_tijd(str(register.opgeslagen(agent)["vervaldatum"]))
    overgangen = (await register.ok("POST", "/beheer/controle", "beheerder"))["overgangen"]
    [overgang] = [o for o in overgangen if o["agent"] == agent]
    assert (overgang["van"], overgang["naar"], overgang["reden"]) == (
        "actief",
        "verlopen",
        "vervaldatum_bereikt",
    )
    assert parse_tijd(overgang["sinds"]) == vervaldatum
    e = register.opgeslagen(agent)
    assert (e["levenscyclusStatus"], parse_tijd(str(e["statusSinds"]))) == ("verlopen", vervaldatum)
    assert not any(t.user == f"agent:{agent}" for t in register.fga.tuples)
    record = await register.record(agent)
    assert record["active"] is False


def _agentrecord(**ext: object) -> dict[str, object]:
    return {
        "id": "a1",
        "userName": "agent",
        "userType": "agent",
        URN_AGENT: {
            "eigenaar": {"value": "m1"},
            "vervaldatum": "2027-01-01T00:00:00Z",
            "levenscyclusStatus": "actief",
            "statusReden": "bevestigd_door_eigenaar",
            **ext,
        },
    }


def test_herbeoordeling_verstreken_voor_controle() -> None:
    volgende = datetime(2026, 11, 1, tzinfo=UTC)
    agent = _agentrecord(volgendeHerbeoordeling=iso(volgende), bevestigingstermijn="P14D")
    # Een dag na het verstrijken, zonder controlerun: de status volgt uit de tijdregels.
    e = _ext(effectief(agent, volgende + timedelta(days=1), "P14D"))
    assert (e["levenscyclusStatus"], e["statusReden"]) == (
        "herbeoordeling_vereist",
        "periodieke_herbeoordeling",
    )
    assert parse_tijd(str(e["statusSinds"])) == volgende
    assert parse_tijd(str(e["bevestigingsdeadline"])) == volgende + timedelta(days=14)
    # De deadline loopt vanaf het verstrijken, niet vanaf het moment van kijken.
    later = _ext(effectief(agent, volgende + timedelta(days=14, seconds=1), "P14D"))
    assert (later["levenscyclusStatus"], later["statusReden"]) == (
        "gedeactiveerd",
        "niet_herbeoordeeld",
    )
    assert parse_tijd(str(later["statusSinds"])) == volgende + timedelta(days=14)
    assert _ext(agent)["levenscyclusStatus"] == "actief"


def test_noodstop_dominant_in_tijdregels() -> None:
    agent = _agentrecord(
        levenscyclusStatus="gedeactiveerd",
        statusReden="uitgeschakeld_door_beheerder",
        vervaldatum="2026-01-01T00:00:00Z",
    )
    resultaat = effectief(agent, datetime(2027, 1, 1, tzinfo=UTC), "P14D")
    e = _ext(resultaat)
    assert (e["levenscyclusStatus"], e["statusReden"]) == (
        "gedeactiveerd",
        "uitgeschakeld_door_beheerder",
    )
    assert resultaat["active"] is False


async def test_noodstop_dominant(register: Register) -> None:
    eigenaar, team, agent = await _actieve_agent(register)
    pad = f"/scim/v2/Users/{agent}"
    uit = [{"op": "replace", "path": "active", "value": False}]
    assert (await register.patch(pad, "beheerder", uit)).status_code == 200
    nieuwe_eigenaar = await register.mens("kees")
    wijzigingen = [
        # Mover van de eigenaar.
        ("bron", f"/scim/v2/Groups/{team}", [{"op": "remove", "path": "members"}]),
        # Eigenaarswissel en nieuwe vervaldatum door de beheerder.
        (
            "beheerder",
            pad,
            [
                {
                    "op": "replace",
                    "path": f"{URN_AGENT}:eigenaar",
                    "value": {"value": nieuwe_eigenaar},
                }
            ],
        ),
        (
            "beheerder",
            pad,
            [
                {
                    "op": "replace",
                    "path": f"{URN_AGENT}:vervaldatum",
                    "value": iso(register.dienst.klok.nu() + timedelta(days=90)),
                }
            ],
        ),
    ]
    for rol, doel, operaties in wijzigingen:
        assert (await register.patch(doel, rol, operaties)).status_code == 200, doel
        e = register.opgeslagen(agent)
        assert (e["levenscyclusStatus"], e["statusReden"]) == (
            "gedeactiveerd",
            "uitgeschakeld_door_beheerder",
        ), doel
    assert (await register.bevestig(agent)).status_code == 409
    assert eigenaar != nieuwe_eigenaar


async def test_bevestiging_is_herleidbaar_na_een_latere_wijziging(register: Register) -> None:
    eigenaar = await register.mens("sanne")
    agent = await register.agent(eigenaar, toegestaneTools=["dossier_lezen", "notitie_toevoegen"])
    gelezen = await register.record(agent)
    assert (await register.bevestig(agent)).status_code == 200
    antwoord = await register.patch(
        f"/scim/v2/Users/{agent}",
        "beheerder",
        [
            {"op": "replace", "path": f"{URN_AGENT}:doel", "value": "dossiers wijzigen"},
            {"op": "replace", "path": f"{URN_AGENT}:toegestaneTools", "value": ["dossier_lezen"]},
        ],
    )
    assert antwoord.status_code == 200
    assert register.opgeslagen(agent)["doel"] == "dossiers wijzigen"

    [bevestigd] = [
        r
        for r in register.dienst.opslag.logboek()
        if r.get("agent") == agent and r.get("gebeurtenis") == "bevestigd"
    ]
    e, inst = _ext(gelezen), register.dienst.inst
    assert bevestigd["bevestigdeVersie"] == gelezen["meta"]["version"]
    assert bevestigd["configuratie"] == {
        "doel": "dossiers samenvatten",
        "vervaldatum": e["vervaldatum"],
        "toegestaneTools": ["dossier_lezen", "notitie_toevoegen"],
        "publiekeSleutel": e["publiekeSleutel"],
        "herbeoordelingTermijn": inst.standaard_herbeoordeling,
        "bevestigingstermijn": inst.standaard_bevestigingstermijn,
    }
    assert set(bevestigd["configuratie"]) == set(AGENT_CONFIGURATIE)


async def test_uitgeschakeld_aangemaakte_agent_vraagt_eerst_de_beheerder(
    register: Register,
) -> None:
    eigenaar = await register.mens("sanne")
    agent = await register.agent(eigenaar, active=False)
    record = await register.record(agent)
    assert record["active"] is False
    assert (_ext(record)["levenscyclusStatus"], _ext(record)["statusReden"]) == (
        "gedeactiveerd",
        "uitgeschakeld_door_beheerder",
    )
    assert (await register.bevestig(agent)).status_code == 409
    assert register.opgeslagen(agent)["levenscyclusStatus"] == "gedeactiveerd"

    aan = [{"op": "replace", "path": "active", "value": True}]
    assert (await register.patch(f"/scim/v2/Users/{agent}", "beheerder", aan)).status_code == 200
    assert register.opgeslagen(agent)["statusReden"] == "heractiveerd_door_beheerder"
    assert (await register.bevestig(agent)).status_code == 200
    assert register.opgeslagen(agent)["levenscyclusStatus"] == "actief"


async def test_bevestiging_synchroniseert_teams(register: Register) -> None:
    eigenaar = await register.mens("sanne")
    await register.team("werk", eigenaar)
    agent = await register.agent(eigenaar)
    # Een mover liet door een fout een tuple op een oud team staan.
    achtergebleven = Tuple(f"gebruiker:{eigenaar}", "lid", "team:oud-team")
    register.fga.tuples.add(achtergebleven)
    assert (await register.bevestig(agent)).status_code == 200
    teams = {
        t.object
        for t in register.fga.tuples
        if t.user == f"gebruiker:{eigenaar}" and t.object.startswith("team:")
    }
    assert achtergebleven.object not in teams
    assert len(teams) == 1


async def _niet_herbeoordeeld(register: Register, controle: bool) -> tuple[str, str, str]:
    """Actieve agent waarvan de bevestigingstermijn na de herbeoordeling al is verstreken.

    Opgeslagen staat hij nog op `actief`, effectief is hij `gedeactiveerd`; met `controle` heeft de
    controle dat al opgeslagen.
    """
    eigenaar, team, agent = await _actieve_agent(
        register, herbeoordelingTermijn="P1D", bevestigingstermijn="P1D"
    )
    register.dienst.klok.zet_vooruit(timedelta(days=3))
    if controle:
        await register.ok("POST", "/beheer/controle", "beheerder")
    verwacht = "gedeactiveerd" if controle else "actief"
    assert register.opgeslagen(agent)["levenscyclusStatus"] == verwacht
    return eigenaar, team, agent


def _status(register: Register, agent: str) -> tuple[object, object]:
    e = register.opgeslagen(agent)
    return e["levenscyclusStatus"], e["statusReden"]


@pytest.mark.parametrize("controle", [False, True])
async def test_groepswijziging_heractiveert_een_verlopen_bevestiging_niet(
    register: Register, controle: bool
) -> None:
    eigenaar, team, agent = await _niet_herbeoordeeld(register, controle)
    verwijder = [{"op": "remove", "path": f'members[value eq "{eigenaar}"]'}]
    assert (await register.patch(f"/scim/v2/Groups/{team}", "bron", verwijder)).status_code == 200
    assert _status(register, agent) == ("gedeactiveerd", "niet_herbeoordeeld")
    assert (await register.bevestig(agent)).status_code == 409


@pytest.mark.parametrize("controle", [False, True])
async def test_configuratiewijziging_heractiveert_een_verlopen_bevestiging_niet(
    register: Register, controle: bool
) -> None:
    _, _, agent = await _niet_herbeoordeeld(register, controle)
    tools = [{"op": "replace", "path": f"{URN_AGENT}:toegestaneTools", "value": ["dossier_lezen"]}]
    assert (await register.patch(f"/scim/v2/Users/{agent}", "beheerder", tools)).status_code == 200
    assert _status(register, agent) == ("gedeactiveerd", "niet_herbeoordeeld")
    assert (await register.bevestig(agent)).status_code == 409


@pytest.mark.parametrize("controle", [False, True])
async def test_leaver_laat_een_verlopen_agent_verlopen(register: Register, controle: bool) -> None:
    eigenaar, _, agent = await _actieve_agent(register, dagen=1)
    register.dienst.klok.zet_vooruit(timedelta(days=2))
    if controle:
        await register.ok("POST", "/beheer/controle", "beheerder")
    uit = [{"op": "replace", "path": "active", "value": False}]
    assert (await register.patch(f"/scim/v2/Users/{eigenaar}", "bron", uit)).status_code == 200
    assert _status(register, agent) == ("verlopen", "vervaldatum_bereikt")
