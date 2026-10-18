"""SCIM 2.0-conformiteit van het profiel (RFC 7643/7644): wat binnen het profiel valt, werkt
volgens de RFC; wat erbuiten valt, wordt herkenbaar geweigerd en wijzigt niets."""

from __future__ import annotations

from typing import Any

import httpx2
import pytest

from levenscyclus.assertie import Sleutelpaar
from levenscyclus.beheer import agentresource
from levenscyclus.register.scim import URN_AGENT, URN_ERROR, URN_GROUP, URN_PATCH, URN_USER
from tests.unit.conftest import Register

USERS = "/scim/v2/Users"
GROUPS = "/scim/v2/Groups"


def _fout(antwoord: httpx2.Response, status: int, scim_type: str | None = None) -> None:
    assert antwoord.status_code == status, antwoord.text
    fout = antwoord.json()
    assert fout["schemas"] == [URN_ERROR]
    assert fout["status"] == str(status)
    assert fout.get("scimType") == scim_type


async def _gebruikers(register: Register) -> list[str]:
    lijst = await register.ok("GET", USERS, "bron")
    return [u["userName"] for u in lijst["Resources"]]


# Strikt profiel (AL-03) ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "naam"),
    [
        ({"password": "geheim"}, "password"),
        ({"name": {"givenName": "Jan"}}, "name"),
        ({"emails": [{"value": "jan@example.org"}]}, "emails"),
        ({"urn:ietf:params:scim:schemas:extension:enterprise:2.0:User": {}}, "enterprise"),
    ],
)
async def test_attribuut_buiten_profiel_geweigerd_en_niet_bewaard(
    register: Register, extra: dict[str, Any], naam: str
) -> None:
    antwoord = await register.verzoek("POST", USERS, "bron", {"userName": "jan", **extra})
    _fout(antwoord, 400, "invalidValue")
    assert naam in antwoord.json()["detail"]
    assert await _gebruikers(register) == []


async def test_null_is_niet_toegekend_en_server_attributen_worden_genegeerd(
    register: Register,
) -> None:
    body = {
        "schemas": ["urn:verkeerd"],
        "id": "zelfgekozen",
        "meta": {"version": 'W/"99"'},
        "userName": "jan",
        "displayName": None,
    }
    user = await register.ok("POST", USERS, "bron", body)
    assert user["id"] != "zelfgekozen"
    assert user["schemas"] == [URN_USER]
    assert user["meta"]["version"] == 'W/"1"'
    assert "displayName" not in user


@pytest.mark.parametrize("veld", ["userName", "displayName", "externalId", "userType"])
async def test_tekstattribuut_moet_string_zijn(register: Register, veld: str) -> None:
    body: dict[str, Any] = {"userName": "jan", veld: 42}
    _fout(await register.verzoek("POST", USERS, "bron", body), 400, "invalidValue")


async def test_groep_met_onbekend_attribuut_geweigerd(register: Register) -> None:
    body = {"displayName": "werk", "owner": "jan"}
    _fout(await register.verzoek("POST", GROUPS, "bron", body), 400, "invalidValue")


# Canonieke namen (AL-13) --------------------------------------------------------------------


async def test_usertype_en_subattributen_canoniek_opgeslagen(register: Register) -> None:
    eigenaar = await register.mens("kees")
    agent = await register.agent(eigenaar)
    record = await register.record(agent)
    body = {k: v for k, v in record.items() if k not in ("id", "meta", "schemas")}
    body["userType"] = "AGENT"
    body[URN_AGENT]["eigenaar"] = {"Value": eigenaar}
    antwoord = await register.verzoek("PUT", f"{USERS}/{agent}", "beheerder", body)
    assert antwoord.status_code == 200, antwoord.text
    opgeslagen = register.dienst.opslag.haal("User", agent)
    assert opgeslagen is not None
    assert opgeslagen["userType"] == "agent"
    assert opgeslagen[URN_AGENT]["eigenaar"] == {"value": eigenaar}


async def test_patchop_sleutels_hoofdletterongevoelig(register: Register) -> None:
    jan = await register.mens("jan")
    piet = await register.mens("piet")
    team = await register.team("werk", jan)
    body = {
        "Schemas": [URN_PATCH.upper()],
        "operations": [{"OP": "Add", "PATH": "Members", "Value": [{"VALUE": piet}]}],
    }
    groep = await register.ok("PATCH", f"{GROUPS}/{team}", "bron", body)
    assert [m["value"] for m in groep["members"]] == [jan, piet]


# Structuur van het PatchOp-bericht (AL-10) --------------------------------------------------


@pytest.mark.parametrize(
    ("body", "scim_type"),
    [
        ({"Operations": [{"op": "remove", "path": "displayName"}]}, "invalidSyntax"),
        ({"schemas": URN_PATCH, "Operations": []}, "invalidSyntax"),
        ({"schemas": [URN_PATCH], "Operations": []}, "invalidSyntax"),
        ({"schemas": [URN_PATCH], "Operations": {"op": "add"}}, "invalidSyntax"),
        ({"schemas": [URN_PATCH], "Operations": ["remove"]}, "invalidSyntax"),
        ({"schemas": [URN_PATCH], "Operations": [{"op": "move", "path": "x"}]}, "invalidSyntax"),
        ({"schemas": [URN_PATCH], "Operations": [{"op": "replace"}]}, "invalidSyntax"),
        (
            {"schemas": [URN_PATCH], "Operations": [{"op": "add", "path": "x", "value": None}]},
            "invalidValue",
        ),
        (
            {"schemas": [URN_PATCH], "Operations": [{"op": "remove", "path": 5}]},
            "invalidPath",
        ),
    ],
)
async def test_ongeldig_patchop_bericht(
    register: Register, body: dict[str, Any], scim_type: str
) -> None:
    jan = await register.mens("jan")
    _fout(await register.verzoek("PATCH", f"{USERS}/{jan}", "bron", body), 400, scim_type)


async def test_patch_is_atomair(register: Register) -> None:
    jan = await register.mens("jan")
    operaties = [
        {"op": "replace", "path": "displayName", "value": "Jan"},
        {"op": "replace", "path": "nickName", "value": "jj"},
    ]
    _fout(await register.patch(f"{USERS}/{jan}", "bron", operaties), 400, "invalidPath")
    assert "displayName" not in await register.ok("GET", f"{USERS}/{jan}", "bron")


# Mutability en paden (AL-11) ----------------------------------------------------------------


async def test_remove_active_heractiveert_een_leaver_niet(register: Register) -> None:
    jan = await register.mens("jan")
    uit = [{"op": "replace", "path": "active", "value": False}]
    assert (await register.patch(f"{USERS}/{jan}", "bron", uit)).status_code == 200
    weg = [{"op": "remove", "path": "active"}]
    _fout(await register.patch(f"{USERS}/{jan}", "bron", weg), 400, "mutability")
    assert (await register.ok("GET", f"{USERS}/{jan}", "bron"))["active"] is False


@pytest.mark.parametrize(
    "operatie",
    [
        {"op": "remove", "path": "userName"},
        {"op": "replace", "path": "userName", "value": None},
        {"op": "replace", "path": "id", "value": "ander-id"},
        {"op": "remove", "path": "id"},
        {"op": "replace", "path": "groups", "value": [{"value": "x"}]},
        {"op": "replace", "path": f"{URN_USER}:groups", "value": []},
    ],
)
async def test_verplicht_en_readonly_geven_mutability(
    register: Register, operatie: dict[str, Any]
) -> None:
    jan = await register.mens("jan")
    await register.team("werk", jan)
    _fout(await register.patch(f"{USERS}/{jan}", "bron", [operatie]), 400, "mutability")


async def test_readonly_met_ongewijzigde_waarde_mag(register: Register) -> None:
    jan = await register.mens("jan")
    team = await register.team("werk", jan)
    gelezen = await register.ok("GET", f"{USERS}/{jan}", "bron")
    operaties = [
        {"op": "replace", "path": "id", "value": jan},
        {"op": "replace", "path": "groups", "value": gelezen["groups"]},
        {"op": "replace", "path": "meta", "value": {"version": "genegeerd"}},
        {"op": "replace", "path": "displayName", "value": "Jan"},
    ]
    user = await register.ok("PATCH", f"{USERS}/{jan}", "bron", _patch(operaties))
    assert user["displayName"] == "Jan"
    assert [g["value"] for g in user["groups"]] == [team]


@pytest.mark.parametrize(
    ("pad", "waarde"),
    [
        (f"{URN_AGENT}:levenscyclusStatus", "actief"),
        (f"{URN_AGENT}:bevestigdeVersie", 'W/"1"'),
    ],
)
async def test_readonly_in_de_extensie_geeft_mutability(
    register: Register, pad: str, waarde: str
) -> None:
    agent = await register.agent(await register.mens("kees"))
    operatie = [{"op": "replace", "path": pad, "value": waarde}]
    _fout(await register.patch(f"{USERS}/{agent}", "beheerder", operatie), 400, "mutability")


@pytest.mark.parametrize("veld", ["doel", "eigenaar", "risicoklasse", "publiekeSleutel"])
async def test_verplicht_extensieattribuut_kan_niet_weg(register: Register, veld: str) -> None:
    agent = await register.agent(await register.mens("kees"))
    operatie = [{"op": "remove", "path": f"{URN_AGENT}:{veld}"}]
    _fout(await register.patch(f"{USERS}/{agent}", "beheerder", operatie), 400, "mutability")


async def test_extensiepad_met_versienummer_in_urn(register: Register) -> None:
    # De extensie-URN eindigt op `:1.0`; het pad erna is het attribuut, ook in andere schrijfwijze.
    agent = await register.agent(await register.mens("kees"))
    pad = f"{URN_AGENT.upper()}:Doel"
    operatie = [{"op": "replace", "path": pad, "value": "notities voorbereiden"}]
    antwoord = await register.patch(f"{USERS}/{agent}", "beheerder", operatie)
    assert antwoord.status_code == 200, antwoord.text
    assert register.opgeslagen(agent)["doel"] == "notities voorbereiden"


async def test_remove_van_ontbrekend_optioneel_attribuut_slaagt(register: Register) -> None:
    jan = await register.mens("jan")
    weg = [{"op": "remove", "path": "externalId"}]
    assert (await register.patch(f"{USERS}/{jan}", "bron", weg)).status_code == 200


@pytest.mark.parametrize(
    ("operatie", "scim_type"),
    [
        ({"op": "remove"}, "noTarget"),
        ({"op": "replace", "path": "nickName", "value": "jj"}, "invalidPath"),
        ({"op": "replace", "path": "name.givenName", "value": "Jan"}, "invalidPath"),
        ({"op": "replace", "path": f"{URN_AGENT}:doel", "value": "x"}, "invalidPath"),
        (
            {
                "op": "replace",
                "path": "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User:manager",
                "value": "x",
            },
            "invalidPath",
        ),
        ({"op": "add", "value": "geen object"}, "invalidValue"),
    ],
)
async def test_pad_buiten_profiel(
    register: Register, operatie: dict[str, Any], scim_type: str
) -> None:
    jan = await register.mens("jan")
    _fout(await register.patch(f"{USERS}/{jan}", "bron", [operatie]), 400, scim_type)


async def test_leden_verwijderen_en_vervangen(register: Register) -> None:
    jan, piet, kees = [await register.mens(n) for n in ("jan", "piet", "kees")]
    team = await register.team("werk", jan, piet)
    niet_lid = [{"op": "remove", "path": f'members[value eq "{kees}"]'}]
    groep = await register.ok("PATCH", f"{GROUPS}/{team}", "bron", _patch(niet_lid))
    assert [m["value"] for m in groep["members"]] == [jan, piet]
    vervang = [{"op": "replace", "path": "members", "value": [{"value": kees}]}]
    groep = await register.ok("PATCH", f"{GROUPS}/{team}", "bron", _patch(vervang))
    assert [m["value"] for m in groep["members"]] == [kees]
    leeg = [{"op": "remove", "path": "members"}]
    groep = await register.ok("PATCH", f"{GROUPS}/{team}", "bron", _patch(leeg))
    assert groep["members"] == []


def _patch(operaties: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schemas": [URN_PATCH], "Operations": operaties}


async def test_rolwissel_is_mutability(register: Register) -> None:
    agent = await register.agent(await register.mens("kees"))
    body = {"userName": "kees-agent-als-mens", "userType": "medewerker"}
    _fout(await register.verzoek("PUT", f"{USERS}/{agent}", "beheerder", body), 400, "mutability")


# Filter (AL-12) -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filter_", "verwacht"),
    [
        ('userName eq "jan and piet"', ["jan and piet"]),
        ('USERNAME EQ "JAN AND PIET"', ["jan and piet"]),
        ('userName eq "a\\"b"', ['a"b']),
        (f'{URN_USER}:userName eq "kees"', ["kees"]),
        ('userName eq "kees" AND displayName eq "Kees"', ["kees"]),
        ('externalId eq "abc"', []),
        ('externalId eq "ABC"', ["kees"]),
    ],
)
async def test_filter(register: Register, filter_: str, verwacht: list[str]) -> None:
    await register.mens("jan and piet")
    await register.mens('a"b')
    body = {"userName": "kees", "displayName": "Kees", "externalId": "ABC"}
    await register.ok("POST", USERS, "bron", body)
    lijst = await register.http.get(
        USERS, params={"filter": filter_}, headers=register.koppen("bron")
    )
    assert lijst.status_code == 200, lijst.text
    assert [u["userName"] for u in lijst.json()["Resources"]] == verwacht


@pytest.mark.parametrize(
    "filter_", ['userName co "j"', "userName eq jan", 'userName eq "j" or userName eq "k"', "x"]
)
async def test_filter_buiten_profiel(register: Register, filter_: str) -> None:
    lijst = await register.http.get(
        USERS, params={"filter": filter_}, headers=register.koppen("bron")
    )
    _fout(lijst, 400, "invalidFilter")


# Projectie en paginering (AL-14) ------------------------------------------------------------


async def _get(register: Register, pad: str, **params: str) -> httpx2.Response:
    return await register.http.get(pad, params=params, headers=register.koppen("bron"))


async def test_projectie(register: Register) -> None:
    jan = await register.mens("jan")
    alleen = await _get(register, f"{USERS}/{jan}", attributes="USERNAME")
    assert set(alleen.json()) == {"id", "schemas", "userName"}
    assert alleen.headers["ETag"] == 'W/"1"'
    zonder = await _get(register, f"{USERS}/{jan}", excludedAttributes=f"{URN_USER}:meta")
    assert "meta" not in zonder.json()
    assert "userName" in zonder.json()


@pytest.mark.parametrize(
    "params",
    [
        {"attributes": "userName", "excludedAttributes": "meta"},
        {"attributes": "name.givenName"},
        {"excludedAttributes": "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"},
    ],
)
async def test_ongeldige_projectie_wijzigt_niets(
    register: Register, params: dict[str, str]
) -> None:
    antwoord = await register.http.post(
        USERS, params=params, json={"userName": "jan"}, headers=register.koppen("bron")
    )
    _fout(antwoord, 400, "invalidValue")
    assert await _gebruikers(register) == []


async def test_projectie_op_schrijfantwoord(register: Register) -> None:
    antwoord = await register.http.post(
        USERS,
        params={"attributes": "userName"},
        json={"userName": "jan"},
        headers=register.koppen("bron"),
    )
    assert antwoord.status_code == 201
    assert set(antwoord.json()) == {"id", "schemas", "userName"}
    assert antwoord.headers["Location"].endswith(antwoord.json()["id"])


@pytest.mark.parametrize(
    "params", [{"sortBy": "userName"}, {"startIndex": "een"}, {"count": "1.5"}]
)
async def test_lijstparameters_buiten_profiel(register: Register, params: dict[str, str]) -> None:
    _fout(await _get(register, USERS, **params), 400, "invalidValue")


async def test_paginering_grenzen(register: Register) -> None:
    for naam in ("a", "b", "c"):
        await register.mens(naam)
    nul = (await _get(register, USERS, count="-5")).json()
    assert (nul["totalResults"], nul["itemsPerPage"], nul["Resources"]) == (3, 0, [])
    vanaf = (await _get(register, USERS, startIndex="0", count="2")).json()
    assert (vanaf["startIndex"], [u["userName"] for u in vanaf["Resources"]]) == (1, ["a", "b"])


# ETags (AL-15) ------------------------------------------------------------------------------


async def _met_versie(
    register: Register, methode: str, pad: str, versie: str, body: dict[str, Any] | None = None
) -> httpx2.Response:
    koppen = {**register.koppen("bron"), "If-Match": versie}
    return await register.http.request(methode, pad, json=body, headers=koppen)


async def test_if_match(register: Register) -> None:
    jan = await register.mens("jan")
    pad = f"{USERS}/{jan}"
    oud = await _met_versie(register, "PUT", pad, 'W/"0"', {"userName": "jan", "active": False})
    _fout(oud, 412)
    assert (await register.ok("GET", pad, "bron"))["active"] is True
    goed = await _met_versie(register, "PATCH", pad, 'W/"1"', _patch([_naam("Jan")]))
    assert goed.status_code == 200, goed.text
    sterk = await _met_versie(register, "PATCH", pad, '"2"', _patch([_naam("Jansen")]))
    assert sterk.status_code == 200, sterk.text
    ster = await _met_versie(register, "PATCH", pad, "*", _patch([_naam("J.")]))
    assert ster.headers["ETag"] == 'W/"4"'
    _fout(await _met_versie(register, "DELETE", pad, 'W/"3"'), 412)
    assert (await _met_versie(register, "DELETE", pad, 'W/"1", W/"4"')).status_code == 204


def _naam(waarde: str) -> dict[str, Any]:
    return {"op": "replace", "path": "displayName", "value": waarde}


async def test_if_match_op_groep(register: Register) -> None:
    team = await register.team("werk")
    pad = f"{GROUPS}/{team}"
    _fout(await _met_versie(register, "PUT", pad, 'W/"2"', {"displayName": "zorg"}), 412)
    assert (await register.ok("GET", pad, "bron"))["displayName"] == "werk"


async def test_if_none_match_geeft_altijd_de_actuele_representatie(register: Register) -> None:
    # Bewust niet ondersteund (ONTWERP §4.1): afgeleide velden vallen buiten de versie.
    jan = await register.mens("jan")
    koppen = {**register.koppen("bron"), "If-None-Match": 'W/"1"'}
    antwoord = await register.http.get(f"{USERS}/{jan}", headers=koppen)
    assert antwoord.status_code == 200


# Discovery en niet-ondersteunde endpoints (AL-16) -------------------------------------------


async def test_discovery_losse_resources_met_absolute_locatie(register: Register) -> None:
    schema = (await register.http.get(f"/scim/v2/Schemas/{URN_AGENT}")).json()
    assert schema["id"] == URN_AGENT
    assert schema["meta"]["location"] == f"http://register.test/scim/v2/Schemas/{URN_AGENT}"
    soort = (await register.http.get("/scim/v2/ResourceTypes/user")).json()
    assert (soort["id"], soort["meta"]["location"]) == (
        "User",
        "http://register.test/scim/v2/ResourceTypes/User",
    )
    spc = (await register.http.get("/scim/v2/ServiceProviderConfig")).json()
    assert spc["meta"]["location"] == "http://register.test/scim/v2/ServiceProviderConfig"
    groep = (await register.http.get(f"/scim/v2/Schemas/{URN_GROUP}")).json()
    assert groep["name"] == "Group"


async def test_discovery_fouten(register: Register) -> None:
    _fout(await register.http.get("/scim/v2/Schemas/urn:onbekend"), 404)
    _fout(await register.http.get("/scim/v2/ResourceTypes/Agent"), 404)
    gefilterd = await register.http.get("/scim/v2/Schemas", params={"filter": 'id eq "x"'})
    _fout(gefilterd, 403)


@pytest.mark.parametrize(
    ("methode", "pad", "status"),
    [
        ("GET", "/scim/v2/Me", 501),
        ("POST", "/scim/v2/Bulk", 501),
        ("POST", "/scim/v2/.search", 501),
        ("POST", "/scim/v2/Users/.search", 501),
        ("POST", "/scim/v2/Groups/.search", 501),
        ("GET", "/scim/v2/Onbekend", 404),
        ("PATCH", USERS, 405),
    ],
)
async def test_buiten_profiel_geeft_scim_fout(
    register: Register, methode: str, pad: str, status: int
) -> None:
    antwoord = await register.http.request(methode, pad, headers=register.koppen("bron"))
    _fout(antwoord, status)
    assert antwoord.headers["content-type"].startswith("application/scim+json")


# Verwijderde medewerker (AL-17) -------------------------------------------------------------


async def test_verwijderde_medewerker_verdwijnt_uit_groepen(register: Register) -> None:
    jan = await register.mens("jan")
    piet = await register.mens("piet")
    team = await register.team("werk", jan, piet)
    assert any(t.user == f"gebruiker:{jan}" for t in register.fga.tuples)
    assert (await register.verzoek("DELETE", f"{USERS}/{jan}", "bron")).status_code == 204
    groep = await register.ok("GET", f"{GROUPS}/{team}", "bron")
    assert [m["value"] for m in groep["members"]] == [piet]
    kees = await register.mens("kees")
    erbij = [{"op": "add", "path": "members", "value": [{"value": kees}]}]
    assert (await register.patch(f"{GROUPS}/{team}", "bron", erbij)).status_code == 200
    uitkomsten = [
        (r["resource"], r["uitkomst"])
        for r in register.dienst.opslag.logboek()
        if r["soort"] == "scim" and r["operatie"] == "DELETE"
    ]
    assert uitkomsten == [("Group werk", "lid_verwijderd"), ("User jan", "ok")]
    assert not any(t.user == f"gebruiker:{jan}" for t in register.fga.tuples)


# Grenzen van tijden en termijnen ------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"vervaldatum": "0001-01-01T00:00:00+01:00"},
        {"herbeoordelingTermijn": "P" + "9" * 400 + "D"},
        {"bevestigingstermijn": "P36526D"},
        {"bevestigingstermijn": 14},
    ],
)
async def test_ongeldige_tijd_of_termijn_is_invalid_value(
    register: Register, extra: dict[str, Any]
) -> None:
    eigenaar = await register.mens("kees")
    body = agentresource(
        "agent-grens",
        eigenaar,
        Sleutelpaar.nieuw().publieke_jwk(),
        {
            "doel": "dossiers samenvatten",
            "vervaldatum": "2099-01-01T00:00:00Z",
            "risicoklasse": "laag",
            **extra,
        },
    )
    _fout(await register.verzoek("POST", USERS, "beheerder", body), 400, "invalidValue")


async def test_demoklok_voorbij_maximum_is_400(register: Register) -> None:
    assert (
        await register.verzoek("POST", "/beheer/klok", "beheerder", {"vooruit": "P36525D"})
    ).status_code == 200
    te_ver = await register.verzoek("POST", "/beheer/klok", "beheerder", {"vooruit": "PT1S"})
    _fout(te_ver, 400, "invalidValue")
