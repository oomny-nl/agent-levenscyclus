"""SCIM-profiel aan de randen: hoofdletters, discovery, noodstop en verouderde bevestiging."""

from __future__ import annotations

import json
from datetime import timedelta

from levenscyclus.register.scim import URN_AGENT, URN_LIST
from tests.unit.conftest import Register


async def _bevestigde_agent(register: Register, dagen: float = 30) -> tuple[str, str, str]:
    eigenaar = await register.mens(f"mw-{len(register.tokens)}")
    team = await register.team(f"team-{len(register.tokens)}", eigenaar)
    agent = await register.agent(eigenaar, dagen=dagen)
    assert (await register.bevestig(agent)).status_code == 200
    return eigenaar, team, agent


def _status(register: Register, agent: str) -> tuple[str, str]:
    e = register.opgeslagen(agent)
    return str(e["levenscyclusStatus"]), str(e["statusReden"])


async def test_leaver_met_hoofdlettervariant(register: Register) -> None:
    # RFC 7643 §2.1: attribuutnamen zijn hoofdletterongevoelig; `Active` is `active`.
    eigenaar, _, agent = await _bevestigde_agent(register)
    operatie = [{"op": "replace", "path": "Active", "value": False}]
    antwoord = await register.patch(f"/scim/v2/Users/{eigenaar}", "bron", operatie)
    assert antwoord.status_code == 200, antwoord.text
    assert antwoord.json()["active"] is False
    assert "Active" not in antwoord.json()
    assert _status(register, agent) == ("gedeactiveerd", "eigenaar_vertrokken")


async def test_noodstop_met_hoofdlettervariant_zonder_pad(register: Register) -> None:
    _, _, agent = await _bevestigde_agent(register)
    operatie = [{"op": "replace", "value": {"ACTIVE": False}}]
    antwoord = await register.patch(f"/scim/v2/Users/{agent}", "beheerder", operatie)
    assert antwoord.status_code == 200, antwoord.text
    assert _status(register, agent) == ("gedeactiveerd", "uitgeschakeld_door_beheerder")


async def test_attribuut_in_twee_schrijfwijzen_geweigerd(register: Register) -> None:
    antwoord = await register.verzoek(
        "POST", "/scim/v2/Users", "bron", {"userName": "dubbel", "active": True, "Active": False}
    )
    assert antwoord.status_code == 400
    assert antwoord.json()["scimType"] == "invalidValue"


async def test_noodstop_na_leaver_wordt_dominant(register: Register) -> None:
    # Ook bij een al gedeactiveerde agent met een vertrokken eigenaar legt een expliciete
    # noodstop zich vast, zodat heractivering later niet stilzwijgend kan.
    eigenaar, _, agent = await _bevestigde_agent(register)
    uit = [{"op": "replace", "path": "active", "value": False}]
    await register.patch(f"/scim/v2/Users/{eigenaar}", "bron", uit)
    antwoord = await register.patch(f"/scim/v2/Users/{agent}", "beheerder", uit)
    assert antwoord.status_code == 200, antwoord.text
    assert _status(register, agent) == ("gedeactiveerd", "uitgeschakeld_door_beheerder")


async def test_wijziging_van_verlopen_agent_is_geen_noodstop(register: Register) -> None:
    # Effectief verlopen, opgeslagen nog actief: de gelezen `active=false` is geen opdracht.
    _, _, agent = await _bevestigde_agent(register, dagen=1)
    register.dienst.klok.zet_vooruit(timedelta(days=2))
    verleng = [
        {"op": "replace", "path": f"{URN_AGENT}:vervaldatum", "value": "2099-01-01T00:00:00Z"}
    ]
    antwoord = await register.patch(f"/scim/v2/Users/{agent}", "beheerder", verleng)
    assert antwoord.status_code == 200, antwoord.text
    assert _status(register, agent) != ("gedeactiveerd", "uitgeschakeld_door_beheerder")


async def test_put_die_gelezen_record_terugschrijft_schakelt_niet(register: Register) -> None:
    _, _, agent = await _bevestigde_agent(register, dagen=1)
    register.dienst.klok.zet_vooruit(timedelta(days=2))
    record = await register.record(agent)
    assert record["active"] is False
    antwoord = await register.verzoek("PUT", f"/scim/v2/Users/{agent}", "beheerder", record)
    assert antwoord.status_code == 200, antwoord.text
    assert _status(register, agent)[1] != "uitgeschakeld_door_beheerder"


async def test_teamwissel_maakt_gelezen_versie_ongeldig(register: Register) -> None:
    # Eigenaar leest de agent terwijl die op bevestiging wacht; daarna wisselt HR zijn teams.
    eigenaar = await register.mens("kees")
    await register.team("werk", eigenaar)
    agent = await register.agent(eigenaar)
    gelezen = await register.record(agent)
    await register.team("zorg", eigenaar)
    antwoord = await register.verzoek(
        "POST",
        f"/beheer/agents/{agent}/bevestiging",
        eigenaar,
        {"doel": gelezen[URN_AGENT]["doel"], "versie": gelezen["meta"]["version"]},
    )
    assert antwoord.status_code == 409
    assert _status(register, agent) == ("herbeoordeling_vereist", "eigenaar_van_team_gewisseld")
    assert (await register.bevestig(agent)).status_code == 200


async def test_discovery_geeft_listresponse(register: Register) -> None:
    for pad in ("/scim/v2/ResourceTypes", "/scim/v2/Schemas"):
        antwoord = (await register.http.get(pad)).json()
        assert antwoord["schemas"] == [URN_LIST]
        assert antwoord["totalResults"] == len(antwoord["Resources"]) >= 2
    schemas = (await register.http.get("/scim/v2/Schemas")).json()["Resources"]
    assert URN_AGENT in {s["id"] for s in schemas}


async def test_agent_met_punt_buiten_de_kromme_geweigerd(register: Register) -> None:
    eigenaar = await register.mens("sanne")
    agent = await register.agent(eigenaar)
    jwk = json.loads(register.opgeslagen(agent)["publiekeSleutel"])
    jwk["y"] = jwk["x"]
    operatie = [{"op": "replace", "path": f"{URN_AGENT}:publiekeSleutel", "value": json.dumps(jwk)}]
    antwoord = await register.patch(f"/scim/v2/Users/{agent}", "beheerder", operatie)
    assert antwoord.status_code == 400
    assert antwoord.json()["scimType"] == "invalidValue"
    assert "P-256" in antwoord.json()["detail"]
