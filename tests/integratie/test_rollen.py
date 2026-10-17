"""Rolscheiding tussen bron, beheerder, eigenaar en agent (NHI5, NHI10, ASI07, ASI09)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from levenscyclus.beheer import BeheerFout, Scim
from levenscyclus.register.scim import URN_AGENT
from tests.integratie.conftest import INST, Keten, uniek

pytestmark = pytest.mark.integratie


def _status(actie: Callable[[], Any]) -> int:
    """HTTP-status van een geweigerde registeractie; de actie moet falen."""
    with pytest.raises(BeheerFout) as fout:
        actie()
    return fout.value.status


def _scimrecords(keten: Keten, naam: str) -> list[dict[str, Any]]:
    return [
        r
        for r in keten.beheer.logboek()
        if r["soort"] == "scim" and r.get("resource") == f"User {naam}"
    ]


def test_agent_heeft_geen_scim_rechten(keten: Keten) -> None:
    agent = keten.actieve_agent()
    assertie = agent.paar.assertie(agent.id, INST.poort_url)
    als_agent = Scim(INST.scim_url, assertie)
    verlengen = [
        {"op": "replace", "path": f"{URN_AGENT}:vervaldatum", "value": "2099-01-01T00:00:00Z"}
    ]
    assert _status(lambda: als_agent.patch("Users", agent.id, verlengen)) == 401
    assert _status(lambda: als_agent.haal("Users", agent.id)) == 401
    # Ook een assertie voor het register zelf geeft geen rechten: het register kent geen agents.
    eigen_aud = agent.paar.assertie(agent.id, INST.register_url)
    assert _status(lambda: Scim(INST.scim_url, eigen_aud).alle("Users")) == 401


def test_bron_kan_geen_agent_maken(keten: Keten) -> None:
    eigenaar = keten.mens()
    agent = keten.agent(eigenaar)
    naam = uniek("agent")
    body = {**keten.record(agent), "userName": naam}
    body.pop("id")
    assert _status(lambda: keten.bron.maak("Users", body)) == 403
    assert [r["uitkomst"] for r in _scimrecords(keten, naam)] == ["geweigerd_rolscheiding"]
    noodstop = [{"op": "replace", "path": "active", "value": False}]
    assert _status(lambda: keten.bron.patch("Users", agent.id, noodstop)) == 403
    assert _scimrecords(keten, agent.naam)[-1]["uitkomst"] == "geweigerd_rolscheiding"
    assert keten.ext(agent)["levenscyclusStatus"] == "herbeoordeling_vereist"


def test_beheerder_kan_niet_bevestigen(keten: Keten) -> None:
    agent = keten.agent(keten.mens())
    record = keten.record(agent)
    doel, versie = record[URN_AGENT]["doel"], record["meta"]["version"]
    assert (
        _status(lambda: keten.beheer.bevestig(agent.id, keten.beheerder_token, doel, versie)) == 403
    )
    assert keten.ext(agent)["levenscyclusStatus"] == "herbeoordeling_vereist"


def test_rolwissel_via_usertype_geweigerd(keten: Keten) -> None:
    mens = keten.mens()
    agent = keten.agent(keten.mens())
    # Een volledig geldig agentrecord onder de id van de medewerker: alleen de rolwissel faalt.
    naam_mens = keten.bron.haal("Users", mens)["userName"]
    als_agent = {**keten.record(agent), "userName": naam_mens}
    als_agent.pop("id")
    assert _status(lambda: keten.bron.vervang("Users", mens, als_agent)) == 403
    als_mens = {"userName": agent.naam, "userType": "medewerker"}
    assert _status(lambda: keten.beheerder.vervang("Users", agent.id, als_mens)) == 403
    assert _scimrecords(keten, naam_mens)[-1]["uitkomst"] == "geweigerd_rolwissel"
    assert _scimrecords(keten, agent.naam)[-1]["uitkomst"] == "geweigerd_rolwissel"
    assert keten.ext(agent)["levenscyclusStatus"] == "herbeoordeling_vereist"


def test_eigenaar_moet_mens_zijn(keten: Keten) -> None:
    andere = keten.actieve_agent()
    with pytest.raises(BeheerFout) as fout:
        keten.agent(andere.id)
    assert fout.value.status == 400
    assert "mens" in fout.value.detail


def test_bevestiging_alleen_door_eigenaar(keten: Keten) -> None:
    agent = keten.agent(keten.mens())
    collega = keten.mens()
    assert _status(lambda: keten.bevestig(agent, collega)) == 403
    weigering = [
        r
        for r in keten.beheer.logboek()
        if r["soort"] == "scim" and r.get("operatie") == "bevestiging"
    ][-1]
    assert (weigering["actor"], weigering["uitkomst"]) == ("eigenaar", "geweigerd_niet_eigenaar")
    assert weigering["resource"] == f"User {agent.naam}"
    assert keten.ext(agent)["levenscyclusStatus"] == "herbeoordeling_vereist"


def test_bevestiging_legt_doel_vast(keten: Keten) -> None:
    agent = keten.agent(keten.mens())
    record = keten.record(agent)
    versie = record["meta"]["version"]
    token = keten.eigenaar_token(agent.eigenaar)
    assert _status(lambda: keten.beheer.bevestig(agent.id, token, "alles mag", versie)) == 409
    bevestigd = keten.beheer.bevestig(agent.id, token, record[URN_AGENT]["doel"], versie)
    e = bevestigd[URN_AGENT]
    assert e["levenscyclusStatus"] == "actief"
    assert (e["doel"], e["bevestigdeVersie"]) == (record[URN_AGENT]["doel"], versie)
    assert e["laatsteBevestiging"]


def test_bevestiging_verouderde_versie(keten: Keten) -> None:
    agent = keten.agent(keten.mens())
    oud = keten.record(agent)
    keten.beheerder.patch(
        "Users",
        agent.id,
        [{"op": "replace", "path": f"{URN_AGENT}:risicoklasse", "value": "midden"}],
    )
    token = keten.eigenaar_token(agent.eigenaar)
    doel, versie = oud[URN_AGENT]["doel"], oud["meta"]["version"]
    assert keten.record(agent)["meta"]["version"] != versie
    assert _status(lambda: keten.beheer.bevestig(agent.id, token, doel, versie)) == 409
    assert keten.ext(agent)["levenscyclusStatus"] == "herbeoordeling_vereist"


def test_eigenaarstoken_gebonden_aan_id(keten: Keten) -> None:
    eerste = keten.mens(team=None)
    naam = keten.bron.haal("Users", eerste)["userName"]
    oud_token = keten.eigenaar_token(eerste)
    keten.bron.verwijder("Users", eerste)
    tweede = str(keten.bron.maak("Users", {"userName": naam, "displayName": naam})["id"])
    assert tweede != eerste
    agent = keten.agent(tweede)
    record = keten.record(agent)
    doel, versie = record[URN_AGENT]["doel"], record["meta"]["version"]
    assert _status(lambda: keten.beheer.bevestig(agent.id, oud_token, doel, versie)) == 401
    keten.bevestig(agent)
    assert keten.ext(agent)["levenscyclusStatus"] == "actief"
