"""Register: SCIM 2.0-server, levenscyclusmotor, beheer-API en logboek (FastAPI).

Rolscheiding: de bron (HR/IdP) beheert mensen en groepen, de beheerder agents, de eigenaar
bevestigt, de poort leest agents en schrijft toolcall-records. Alle mutaties lopen achter één
asyncio-lock; SCIM-mutatie en de statuswijziging van betrokken agents delen één SQLite-transactie.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from levenscyclus.config import Instellingen
from levenscyclus.duur import parse_duur
from levenscyclus.fga import Fga, FgaFout, FgaIds, zorg_store
from levenscyclus.klok import Klok, iso
from levenscyclus.register import scim
from levenscyclus.register.motor import Motor, ext
from levenscyclus.register.opslag import LogboekFout, Opslag
from levenscyclus.register.scim import ScimFout, is_agent

log = logging.getLogger("register")
LEZERS = ("bron", "beheerder", "poort")
MUTATOREN = ("bron", "beheerder")
RELATIES = ("lezen", "notitie_schrijven", "besluit_nemen")
MAX_BODY = 256 * 1024


@dataclass(frozen=True)
class Principal:
    """Geauthenticeerde aanroeper: `bron`, `beheerder`, `poort` of `eigenaar` (met SCIM-id)."""

    soort: str
    subject: str | None = None


@dataclass(frozen=True)
class Mutatie:
    """Eén schrijfverzoek op Users: wie, welke operatie en de expliciete `active`-opdracht.

    `schakelaar` is alleen gezet als de beheerder bij een bestaande agent `active` expliciet
    zet (noodstop of heractivering); anders None.
    """

    principal: Principal
    op: str
    schakelaar: bool | None = None


@dataclass
class Dienst:
    """Gedeelde onderdelen van het register.

    Attributes:
        inst: Instellingen.
        klok: Registerklok (demostand via `DEMO_KLOK=1`).
        opslag: SQLite-opslag.
        api_token: Preshared key van OpenFGA.
        model: Autorisatiemodel in JSON-vorm (`openfga/model.json`).
        fga: OpenFGA-toegang (na `start`).
        motor: Levenscyclusmotor (na `start`).
    """

    inst: Instellingen
    klok: Klok
    opslag: Opslag
    api_token: str
    model: dict[str, Any]
    fga: Fga | None = None
    motor: Motor | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    herbouw_open: bool = False

    async def start(self, pogingen: int = 30) -> None:
        """Verifieer het logboek, maak de principal-tokens en zorg voor store en model."""
        uitkomst = self.opslag.verifieer_bij_start()
        log.info("logboek bij start: %s", uitkomst.reden)
        self.klok.herstel(demoklok_uit_logboek(self.opslag.logboek()))
        for naam in LEZERS:
            self.opslag.token_voor(naam, None, self.inst.geheimen_dir / f"{naam}.token")
        for poging in range(pogingen):
            try:
                ids = await self._zorg_ids()
                break
            except FgaFout as fout:
                if poging == pogingen - 1:
                    raise
                log.warning("OpenFGA nog niet gereed (%s); opnieuw over 2 s", fout)
                await asyncio.sleep(2)
        self.fga = Fga(self.inst.openfga_url, self.api_token, ids)
        self.motor = Motor(self.opslag, self.fga, self.klok, self.inst)
        await self.motor.herbouw_alles()

    async def _zorg_ids(self) -> FgaIds:
        return await zorg_store(
            self.inst.openfga_url, self.api_token, self.inst.openfga_store, self.model
        )

    async def herstel_fga(self) -> FgaIds:
        """Store en model controleren of aanmaken; bij nieuwe id's alle tuples herbouwen."""
        ids = await self._zorg_ids()
        if self.f.vernieuw(ids):
            log.warning("OpenFGA-store of model opnieuw aangemaakt; tuples worden herbouwd")
            self.herbouw_open = True
        if self.herbouw_open:
            # Blijft open tot een volledige herbouw slaagt; een halve herbouw wordt hervat.
            await self.m.herbouw_alles()
            self.herbouw_open = False
        return ids

    @property
    def m(self) -> Motor:
        """De motor; alleen na `start`."""
        if self.motor is None:
            raise ScimFout(503, "register start nog")
        return self.motor

    @property
    def f(self) -> Fga:
        """OpenFGA-toegang; alleen na `start`."""
        if self.fga is None:
            raise ScimFout(503, "register start nog")
        return self.fga


# Hulpfuncties ------------------------------------------------------------------------------------


def demoklok_uit_logboek(records: list[dict[str, Any]]) -> timedelta:
    """De demoklok-offset volgens het geverifieerde logboek: de som van alle stappen vooruit."""
    stappen = (
        parse_duur(str(r.get("reden")))
        for r in records
        if r.get("soort") == "levenscyclus" and r.get("gebeurtenis") == "demoklok_vooruit"
    )
    return sum(stappen, timedelta(0))


def _principal(dienst: Dienst, request: Request, *toegestaan: str) -> Principal:
    kop = request.headers.get("authorization", "")
    if not kop.lower().startswith("bearer "):
        raise ScimFout(401, "Bearer-token ontbreekt")
    gevonden = dienst.opslag.principal_van(kop[7:].strip())
    if gevonden is None:
        raise ScimFout(401, "token onbekend")
    principal = Principal(*gevonden)
    if toegestaan and principal.soort not in toegestaan:
        raise ScimFout(403, f"{principal.soort} mag dit niet; toegestaan: {', '.join(toegestaan)}")
    return principal


async def _json(request: Request) -> dict[str, Any]:
    body = await request.body()
    if len(body) > MAX_BODY:
        raise ScimFout(413, "verzoek te groot")
    try:
        gegevens = json.loads(body or b"{}")
    except ValueError as fout:
        raise ScimFout(400, f"geen geldige JSON: {fout}", "invalidSyntax") from fout
    if not isinstance(gegevens, dict):
        raise ScimFout(400, "een JSON-object werd verwacht", "invalidSyntax")
    return gegevens


def _antwoord(resource: dict[str, Any], status: int = 200) -> Response:
    koppen = {}
    if "meta" in resource:
        koppen["ETag"] = resource["meta"]["version"]
        koppen["Location"] = resource["meta"]["location"]
    return JSONResponse(resource, status_code=status, media_type=scim.MEDIA, headers=koppen)


def _log_scim(
    dienst: Dienst, principal: Principal, operatie: str, doel: str, uitkomst: str
) -> None:
    dienst.opslag.log(
        {
            "soort": "scim",
            "actor": principal.soort,
            "operatie": operatie,
            "resource": doel,
            "uitkomst": uitkomst,
        },
        dienst.klok.nu(),
    )


def _controleer_rol(
    dienst: Dienst,
    principal: Principal,
    oud: dict[str, Any] | None,
    nieuw: dict[str, Any],
    operatie: str,
) -> None:
    """Rolscheiding op opgeslagen resource én resultaat; `userType`-klasse is onveranderlijk."""
    klasse = "agent" if is_agent(nieuw) else "mens"
    doel = f"User {nieuw.get('userName', oud['userName'] if oud else '?')}"
    if oud is not None and is_agent(oud) != is_agent(nieuw):
        _log_scim(dienst, principal, operatie, doel, "geweigerd_rolwissel")
        raise ScimFout(403, "userType kan niet wisselen tussen mens en agent", "mutability")
    verwacht = {"bron": "mens", "beheerder": "agent"}.get(principal.soort)
    if verwacht != klasse:
        _log_scim(dienst, principal, operatie, doel, "geweigerd_rolscheiding")
        raise ScimFout(403, f"{principal.soort} mag geen {klasse} beheren")


def _verrijk(dienst: Dienst, resource: dict[str, Any]) -> dict[str, Any]:
    """Uitgaande User: effectieve status voor agents, `groups` voor mensen."""
    if is_agent(resource):
        return dienst.m.effectief(resource)
    resource["groups"] = [
        {"value": g["id"], "display": g["displayName"], "$ref": g["meta"]["location"]}
        for g in dienst.opslag.alle("Group")
        if resource["id"] in scim.leden_van(g)
    ]
    return resource


def _projecteer(resource: dict[str, Any], request: Request) -> dict[str, Any]:
    """`attributes` en `excludedAttributes` (alleen topniveau)."""
    params = request.query_params
    weg = {a.strip() for a in params.get("excludedAttributes", "").split(",") if a.strip()}
    houd = {a.strip() for a in params.get("attributes", "").split(",") if a.strip()}
    vast = {"schemas", "id", "meta"}
    return {
        k: v for k, v in resource.items() if k not in weg and (not houd or k in houd or k in vast)
    }


def _lijst(items: list[dict[str, Any]], request: Request) -> Response:
    params = request.query_params
    paren = scim.parse_filter(params["filter"]) if "filter" in params else []
    gevonden = [r for r in items if scim.voldoet(r, paren)]
    start = max(int(params.get("startIndex", 1)), 1)
    aantal = max(min(int(params.get("count", 100)), 200), 0)
    pagina = gevonden[start - 1 : start - 1 + aantal]
    return JSONResponse(
        {
            "schemas": [scim.URN_LIST],
            "totalResults": len(gevonden),
            "itemsPerPage": len(pagina),
            "startIndex": start,
            "Resources": [_projecteer(r, request) for r in pagina],
        },
        media_type=scim.MEDIA,
    )


def _haal(dienst: Dienst, soort: str, id_: str) -> dict[str, Any]:
    gevonden = dienst.opslag.haal(soort, id_)
    if gevonden is None:
        raise ScimFout(404, f"{soort} {id_} bestaat niet")
    return gevonden


# Users -------------------------------------------------------------------------------------------


async def _schrijf_user(
    dienst: Dienst, mutatie: Mutatie, oud: dict[str, Any] | None, body: dict[str, Any]
) -> dict[str, Any]:
    """Gedeelde route voor POST, PUT en PATCH op Users (binnen de lock)."""
    principal, op = mutatie.principal, mutatie.op
    nieuw = scim.valideer_user(scim.strip_server(body, scim.USER_SERVER))
    _controleer_rol(dienst, principal, oud, nieuw, op)
    nu = iso(dienst.klok.nu())
    if is_agent(nieuw):
        opgeslagen = await _schrijf_agent(dienst, mutatie, oud, nieuw)
    elif oud is None:
        with dienst.opslag.transactie():
            opgeslagen = dienst.opslag.maak("User", nieuw, nu)
            _log_scim(dienst, principal, op, f"User {opgeslagen['userName']}", "ok")
        pad = dienst.inst.geheimen_dir / "eigenaren" / f"{opgeslagen['id']}.token"
        dienst.opslag.token_voor("eigenaar", opgeslagen["id"], pad)
    else:
        with dienst.opslag.transactie():
            opgeslagen = dienst.opslag.vervang("User", oud["id"], nieuw, nu)
            getroffen = dienst.m.blokkeer_na_gebruikerswijziging(oud, opgeslagen)
            _log_scim(dienst, principal, op, f"User {opgeslagen['userName']}", "ok")
        await dienst.m.sync_na_gebruikerswijziging(oud["id"], getroffen)
    return opgeslagen


async def _schrijf_agent(
    dienst: Dienst, mutatie: Mutatie, oud: dict[str, Any] | None, nieuw: dict[str, Any]
) -> dict[str, Any]:
    actor = mutatie.principal.soort
    # Een actieve eigenaar is alleen bij aanmaak vereist; bij een wijziging controleert de motor
    # hem bij eigenaarswissel en heractivering, zodat een noodstop ook na een leaver kan.
    if oud is None:
        dienst.m.controleer_eigenaar(ext(nieuw)["eigenaar"]["value"])
    with dienst.opslag.transactie():
        if oud is None:
            aangemaakt = dienst.opslag.maak("User", nieuw, iso(dienst.klok.nu()))
            opgeslagen = dienst.m.registreer_aanmaak(aangemaakt, actor)
        else:
            opgeslagen = dienst.m.registreer_agentwijziging(oud, nieuw, actor, mutatie.schakelaar)
        _log_scim(dienst, mutatie.principal, mutatie.op, f"User {opgeslagen['userName']}", "ok")
    if oud is None:
        await dienst.m.sync_na_aanmaak(opgeslagen)
    else:
        await dienst.m.sync_na_agentwijziging(opgeslagen)
    return opgeslagen


async def _verwijder_user(dienst: Dienst, principal: Principal, oud: dict[str, Any]) -> None:
    _controleer_rol(dienst, principal, oud, oud, "DELETE")
    with dienst.opslag.transactie():
        if is_agent(oud):
            dienst.m.registreer_verwijdering(oud, principal.soort)
            getroffen: list[dict[str, Any]] = []
        else:
            getroffen = dienst.m.blokkeer_na_gebruikerswijziging(oud, None)
            dienst.opslag.trek_token_in(oud["id"])
        dienst.opslag.verwijder("User", oud["id"])
        _log_scim(dienst, principal, "DELETE", f"User {oud['userName']}", "ok")
    if is_agent(oud):
        await dienst.m.sync_na_verwijdering(oud)
    else:
        await dienst.m.sync_na_gebruikerswijziging(oud["id"], getroffen)


def _schakelaar_uit_put(dienst: Dienst, oud: dict[str, Any], body: dict[str, Any]) -> bool | None:
    """`active` uit een PUT op een agent, alleen als hij afwijkt van de effectieve waarde.

    Een PUT bevat de hele resource; wie de gelezen agent ongewijzigd terugschrijft, geeft geen
    opdracht. Een expliciete noodstop op een al gedeactiveerde agent gaat via PATCH.
    """
    if not is_agent(oud):
        return None
    gewenst = scim.normaliseer_namen(body, scim.KERN).get("active")
    if not isinstance(gewenst, bool):
        return None
    return gewenst if gewenst != dienst.m.effectief(oud)["active"] else None


def _users(app: FastAPI, dienst: Dienst) -> None:
    basis = "/scim/v2/Users"

    @app.get(basis)
    async def lijst(request: Request) -> Response:
        _principal(dienst, request, *LEZERS)
        return _lijst([_verrijk(dienst, u) for u in dienst.opslag.alle("User")], request)

    @app.get(basis + "/{id_}")
    async def een(id_: str, request: Request) -> Response:
        _principal(dienst, request, *LEZERS)
        return _antwoord(_projecteer(_verrijk(dienst, _haal(dienst, "User", id_)), request))

    @app.post(basis)
    async def maak(request: Request) -> Response:
        principal = _principal(dienst, request, *MUTATOREN)
        body = await _json(request)
        async with dienst.lock:
            opgeslagen = await _schrijf_user(dienst, Mutatie(principal, "POST"), None, body)
        return _antwoord(_verrijk(dienst, opgeslagen), 201)

    @app.put(basis + "/{id_}")
    async def vervang(id_: str, request: Request) -> Response:
        principal = _principal(dienst, request, *MUTATOREN)
        body = await _json(request)
        async with dienst.lock:
            oud = _haal(dienst, "User", id_)
            mutatie = Mutatie(principal, "PUT", _schakelaar_uit_put(dienst, oud, body))
            opgeslagen = await _schrijf_user(dienst, mutatie, oud, body)
        return _antwoord(_verrijk(dienst, opgeslagen))

    @app.patch(basis + "/{id_}")
    async def wijzig(id_: str, request: Request) -> Response:
        principal = _principal(dienst, request, *MUTATOREN)
        body = await _json(request)
        async with dienst.lock:
            oud = _haal(dienst, "User", id_)
            huidig = _verrijk(dienst, oud) if is_agent(oud) else dict(oud)
            nieuw = scim.pas_patch_toe(huidig, body)
            mutatie = Mutatie(principal, "PATCH", scim.schakelaar_uit_patch(body))
            opgeslagen = await _schrijf_user(dienst, mutatie, oud, nieuw)
        return _antwoord(_verrijk(dienst, opgeslagen))

    @app.delete(basis + "/{id_}")
    async def verwijder(id_: str, request: Request) -> Response:
        principal = _principal(dienst, request, *MUTATOREN)
        async with dienst.lock:
            await _verwijder_user(dienst, principal, _haal(dienst, "User", id_))
        return Response(status_code=204)


# Groups ------------------------------------------------------------------------------------------


def _controleer_leden(dienst: Dienst, groep: dict[str, Any]) -> None:
    for lid in scim.leden_van(groep):
        gebruiker = dienst.opslag.haal("User", lid)
        if gebruiker is None or is_agent(gebruiker):
            raise ScimFout(400, f"member {lid} is geen bestaande medewerker", "invalidValue")


async def _schrijf_group(
    dienst: Dienst, principal: Principal, oud: dict[str, Any] | None, body: dict[str, Any], op: str
) -> dict[str, Any]:
    nieuw = scim.valideer_group(scim.strip_server(body, scim.GROUP_SERVER))
    _controleer_leden(dienst, nieuw)
    nu = iso(dienst.klok.nu())
    with dienst.opslag.transactie():
        if oud is None:
            opgeslagen = dienst.opslag.maak("Group", nieuw, nu)
        else:
            opgeslagen = dienst.opslag.vervang("Group", oud["id"], nieuw, nu)
        eigenaren, getroffen = dienst.m.blokkeer_na_groepswijziging(oud, opgeslagen)
        _log_scim(dienst, principal, op, f"Group {opgeslagen['displayName']}", "ok")
    await dienst.m.sync_na_groepswijziging(eigenaren, getroffen)
    return opgeslagen


def _groups(app: FastAPI, dienst: Dienst) -> None:
    basis = "/scim/v2/Groups"

    @app.get(basis)
    async def lijst(request: Request) -> Response:
        _principal(dienst, request, *LEZERS)
        return _lijst(dienst.opslag.alle("Group"), request)

    @app.get(basis + "/{id_}")
    async def een(id_: str, request: Request) -> Response:
        _principal(dienst, request, *LEZERS)
        return _antwoord(_projecteer(_haal(dienst, "Group", id_), request))

    @app.post(basis)
    async def maak(request: Request) -> Response:
        principal = _principal(dienst, request, "bron")
        body = await _json(request)
        async with dienst.lock:
            return _antwoord(await _schrijf_group(dienst, principal, None, body, "POST"), 201)

    @app.put(basis + "/{id_}")
    async def vervang(id_: str, request: Request) -> Response:
        principal = _principal(dienst, request, "bron")
        body = await _json(request)
        async with dienst.lock:
            oud = _haal(dienst, "Group", id_)
            return _antwoord(await _schrijf_group(dienst, principal, oud, body, "PUT"))

    @app.patch(basis + "/{id_}")
    async def wijzig(id_: str, request: Request) -> Response:
        principal = _principal(dienst, request, "bron")
        body = await _json(request)
        async with dienst.lock:
            oud = _haal(dienst, "Group", id_)
            nieuw = scim.pas_patch_toe(dict(oud), body)
            return _antwoord(await _schrijf_group(dienst, principal, oud, nieuw, "PATCH"))

    @app.delete(basis + "/{id_}")
    async def verwijder(id_: str, request: Request) -> Response:
        principal = _principal(dienst, request, "bron")
        async with dienst.lock:
            oud = _haal(dienst, "Group", id_)
            with dienst.opslag.transactie():
                dienst.opslag.verwijder("Group", id_)
                eigenaren, getroffen = dienst.m.blokkeer_na_groepswijziging(oud, None)
                _log_scim(dienst, principal, "DELETE", f"Group {oud['displayName']}", "ok")
            await dienst.m.sync_na_groepswijziging(eigenaren, getroffen)
        return Response(status_code=204)


# Discovery ---------------------------------------------------------------------------------------


def _discovery(app: FastAPI) -> None:
    @app.get("/scim/v2/ServiceProviderConfig")
    async def spc() -> Response:
        return JSONResponse(scim.service_provider_config(), media_type=scim.MEDIA)

    @app.get("/scim/v2/ResourceTypes")
    async def resource_types() -> Response:
        return JSONResponse(scim.lijstantwoord(scim.resource_types()), media_type=scim.MEDIA)

    @app.get("/scim/v2/Schemas")
    async def schemas() -> Response:
        schemas = [*scim.kernschemas(), scim.agentschema()]
        return JSONResponse(scim.lijstantwoord(schemas), media_type=scim.MEDIA)


# Beheer ------------------------------------------------------------------------------------------


def _overzichtsregel(dienst: Dienst, agent: dict[str, Any]) -> dict[str, Any]:
    eff = dienst.m.effectief(agent)
    e = ext(eff)
    eigenaar_id = e["eigenaar"]["value"]
    groepen = dienst.opslag.alle("Group")
    return {
        "id": agent["id"],
        "agent": agent["userName"],
        "eigenaar": e["eigenaar"].get("display"),
        "eigenaar_id": eigenaar_id,
        "teams": [g["displayName"] for g in groepen if eigenaar_id in scim.leden_van(g)],
        "doel": e["doel"],
        "risicoklasse": e["risicoklasse"],
        "status": e["levenscyclusStatus"],
        "reden": e["statusReden"],
        "statusSinds": e["statusSinds"],
        "vervaldatum": e["vervaldatum"],
        "laatsteBevestiging": e.get("laatsteBevestiging"),
        "volgendeHerbeoordeling": e.get("volgendeHerbeoordeling"),
        "bevestigingsdeadline": e.get("bevestigingsdeadline"),
        "versie": agent["meta"]["version"],
    }


def _agent(dienst: Dienst, id_: str) -> dict[str, Any]:
    agent = dienst.opslag.haal("User", id_)
    if agent is None or not is_agent(agent):
        raise ScimFout(404, f"agent {id_} bestaat niet")
    return agent


async def _bevestig(
    dienst: Dienst, principal: Principal, id_: str, body: dict[str, Any]
) -> Response:
    if principal.subject is None:
        raise ScimFout(403, "eigenaarstoken zonder subject")
    async with dienst.lock:
        agent = _agent(dienst, id_)
        try:
            opgeslagen = await dienst.m.bevestig(
                agent, principal.subject, str(body.get("doel", "")), str(body.get("versie", ""))
            )
        except ScimFout as fout:
            if fout.status == 403:
                _log_scim(
                    dienst,
                    principal,
                    "bevestiging",
                    f"User {agent['userName']}",
                    "geweigerd_niet_eigenaar",
                )
            raise
    return _antwoord(dienst.m.effectief(opgeslagen))


def _beheer(app: FastAPI, dienst: Dienst) -> None:
    @app.get("/beheer/agents")
    async def overzicht(request: Request) -> Response:
        _principal(dienst, request, "beheerder")
        regels = [_overzichtsregel(dienst, a) for a in dienst.opslag.agents()]
        return JSONResponse({"tijd": iso(dienst.klok.nu()), "agents": regels})

    @app.post("/beheer/controle")
    async def controle(request: Request) -> Response:
        _principal(dienst, request, "beheerder")
        async with dienst.lock:
            overgangen = dienst.m.maak_persistent()
            try:
                await dienst.herstel_fga()
                await dienst.m.herbouw_alles()
            except FgaFout as fout:
                raise ScimFout(503, f"controle onvolledig, tuples niet herbouwd: {fout}") from fout
        return JSONResponse(
            {"tijd": iso(dienst.klok.nu()), "overgangen": [o.__dict__ for o in overgangen]}
        )

    @app.post("/beheer/agents/{id_}/bevestiging")
    async def bevestiging(id_: str, request: Request) -> Response:
        principal = _principal(dienst, request, "eigenaar")
        return await _bevestig(dienst, principal, id_, await _json(request))

    @app.get("/beheer/agents/{id_}/relatiecheck")
    async def relatiecheck(id_: str, request: Request) -> Response:
        _principal(dienst, request, "beheerder")
        return await _relatiecheck(dienst, _agent(dienst, id_), request)

    if dienst.klok.demostand:
        _demoklok(app, dienst)


async def _relatiecheck(dienst: Dienst, agent: dict[str, Any], request: Request) -> Response:
    relatie = request.query_params.get("relatie", "lezen")
    dossier = request.query_params.get("dossier", "")
    if relatie not in RELATIES or not dossier:
        raise ScimFout(400, f"relatie ({'|'.join(RELATIES)}) en dossier zijn verplicht")
    try:
        toegestaan = await dienst.m.relatiecheck(agent, relatie, dossier)
    except FgaFout as fout:
        raise ScimFout(503, str(fout)) from fout
    return JSONResponse(
        {
            "agent": agent["userName"],
            "relatie": relatie,
            "dossier": dossier,
            "huidige_tijd": iso(dienst.klok.nu()),
            "allowed": toegestaan,
        }
    )


def _demoklok(app: FastAPI, dienst: Dienst) -> None:
    @app.post("/beheer/klok")
    async def klok(request: Request) -> Response:
        _principal(dienst, request, "beheerder")
        body = await _json(request)
        try:
            duur = parse_duur(str(body.get("vooruit", "")))
        except ValueError as fout:
            raise ScimFout(400, str(fout), "invalidValue") from fout
        async with dienst.lock:
            nu = dienst.klok.zet_vooruit(duur)
            dienst.opslag.log(
                {
                    "soort": "levenscyclus",
                    "gebeurtenis": "demoklok_vooruit",
                    "actor": "beheerder",
                    "reden": body.get("vooruit"),
                },
                nu,
            )
        return JSONResponse(
            {"tijd": iso(nu), "offset_seconden": dienst.klok.offset.total_seconds()}
        )


# Logboek en OpenFGA ------------------------------------------------------------------------------


async def _schrijf_toolcall(dienst: Dienst, body: dict[str, Any]) -> Response:
    """Toolcall-record van de poort; bij `toegestaan` de definitieve toelating onder het slot."""
    if body.get("soort") != "toolcall":
        raise ScimFout(400, "de poort schrijft alleen records van soort toolcall")
    async with dienst.lock:
        reden = None
        if body.get("beslissing") == "toegestaan":
            reden = dienst.m.controleer_toelating(body)
            if reden is not None:
                body = {**body, "beslissing": "geweigerd", "reden": reden}
        record = dienst.opslag.log(body, dienst.klok.nu())
    return JSONResponse(record, status_code=409 if reden else 201)


def _logboek(app: FastAPI, dienst: Dienst) -> None:
    @app.get("/logboek")
    async def lees(request: Request) -> Response:
        _principal(dienst, request, "beheerder", "poort")
        return JSONResponse({"records": dienst.opslag.logboek()})

    @app.post("/logboek")
    async def schrijf(request: Request) -> Response:
        _principal(dienst, request, "poort")
        return await _schrijf_toolcall(dienst, await _json(request))

    @app.get("/openfga")
    async def openfga(request: Request) -> Response:
        _principal(dienst, request, "beheerder", "poort")
        async with dienst.lock:
            try:
                ids = await dienst.herstel_fga()
            except FgaFout as fout:
                raise ScimFout(503, str(fout)) from fout
        return JSONResponse({"store_id": ids.store_id, "model_id": ids.model_id})

    @app.get("/health")
    async def health() -> Response:
        gereed = dienst.motor is not None and not dienst.opslag.geblokkeerd
        return JSONResponse(
            {"status": "ok" if gereed else "niet gereed", "logboek": dienst.opslag.geblokkeerd},
            status_code=200 if gereed else 503,
        )


def maak_app(dienst: Dienst) -> FastAPI:
    """Bouw de FastAPI-app rond een dienst (tests geven een eigen dienst mee)."""

    @asynccontextmanager
    async def levensduur(_: FastAPI) -> AsyncGenerator[None]:
        if dienst.motor is None:
            await dienst.start()
        yield

    app = FastAPI(title="agent-levenscyclus register", lifespan=levensduur)

    @app.exception_handler(ScimFout)
    async def scimfout(_: Request, fout: ScimFout) -> Response:
        return JSONResponse(fout.resource(), status_code=fout.status, media_type=scim.MEDIA)

    @app.exception_handler(LogboekFout)
    async def logboekfout(_: Request, fout: LogboekFout) -> Response:
        return JSONResponse(
            ScimFout(503, str(fout)).resource(), status_code=503, media_type=scim.MEDIA
        )

    _discovery(app)
    _users(app, dienst)
    _groups(app, dienst)
    _beheer(app, dienst)
    _logboek(app, dienst)
    return app


def maak_dienst(inst: Instellingen | None = None) -> Dienst:
    """Dienst uit de omgeving: preshared key, demoklok (`DEMO_KLOK=1`) en modelbestand."""
    inst = inst or Instellingen.uit_omgeving()
    sleutel = os.environ.get("OPENFGA_PRESHARED_KEY", "")
    if not sleutel:
        raise SystemExit("OPENFGA_PRESHARED_KEY ontbreekt; `make` zet hem in .env")
    model_pad = Path(os.environ.get("OPENFGA_MODEL_PAD", "/app/openfga/model.json"))
    klok = Klok(demostand=os.environ.get("DEMO_KLOK") == "1")
    basis = os.environ.get("REGISTER_BASIS", inst.register_url)
    opslag = Opslag(inst.db_pad, inst.anker_pad, basis)
    return Dienst(inst, klok, opslag, sleutel, json.loads(model_pad.read_text(encoding="utf-8")))


def main() -> None:
    """Start het register."""
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(maak_app(maak_dienst()), host="0.0.0.0", port=8000, log_level="info")  # noqa: S104
