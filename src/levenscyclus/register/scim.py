"""SCIM 2.0-profiel van het register: schema's, filter, PATCH en validatie (RFC 7643/7644).

Bewust beperkt profiel, zie docs/ONTWERP.md §4.1. Alles wat buiten het profiel valt, wordt
herkenbaar geweigerd met een SCIM-foutresource.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any

from levenscyclus.assertie import controleer_jwk
from levenscyclus.duur import parse_duur
from levenscyclus.klok import parse_tijd

URN_USER = "urn:ietf:params:scim:schemas:core:2.0:User"
URN_GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
URN_AGENT = "urn:oomny:scim:schemas:extension:agent:1.0"
URN_LIST = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
URN_PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
URN_ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"
URN_SPC = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
URN_RT = "urn:ietf:params:scim:schemas:core:2.0:ResourceType"
URN_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Schema"
MEDIA = "application/scim+json"

RISICOKLASSEN = ("laag", "midden", "hoog")
FILTERBAAR = ("userName", "displayName", "userType", "externalId", "id")
# `caseExact` (RFC 7643 §2.2, §3.1): deze waarden worden exact vergeleken, de rest niet.
CASE_EXACT = ("id", "externalId")
# Extensieattributen die de beheerder schrijft; de rest is readOnly en door de server bepaald.
AGENT_SCHRIJFBAAR = (
    "eigenaar",
    "doel",
    "vervaldatum",
    "risicoklasse",
    "toegestaneTools",
    "publiekeSleutel",
    "herbeoordelingTermijn",
    "bevestigingstermijn",
)
AGENT_VERPLICHT = ("eigenaar", "doel", "vervaldatum", "risicoklasse", "publiekeSleutel")
AGENT_CONFIGURATIE = (
    "doel",
    "vervaldatum",
    "toegestaneTools",
    "publiekeSleutel",
    "herbeoordelingTermijn",
    "bevestigingstermijn",
)
AGENT_READONLY = (
    "levenscyclusStatus",
    "statusReden",
    "statusSinds",
    "laatsteBevestiging",
    "bevestigdeVersie",
    "volgendeHerbeoordeling",
    "bevestigingsdeadline",
    "beoordeeldOp",
)
AGENT_ATTRIBUTEN = AGENT_SCHRIJFBAAR + AGENT_READONLY
# Canonieke namen van de kernattributen; SCIM-attribuutnamen zijn hoofdletterongevoelig
# (RFC 7643 §2.1), dus `Active` en `active` zijn hetzelfde attribuut.
KERN = (
    "id",
    "meta",
    "schemas",
    "externalId",
    "userName",
    "displayName",
    "userType",
    "active",
    "groups",
    "members",
    URN_AGENT,
)
# Wat een client mag schrijven; al het andere is server-attribuut of valt buiten het profiel.
USER_ATTRIBUTEN = ("externalId", "userName", "displayName", "userType", "active", URN_AGENT)
GROUP_ATTRIBUTEN = ("externalId", "displayName", "members")
USER_SERVER = ("id", "meta", "schemas", "groups")
GROUP_SERVER = ("id", "meta", "schemas")
# Door de server bepaald en bij schrijven genegeerd: `meta` (RFC 7643 §3.1) en `schemas`, dat
# volgt uit `userType`.
GENEGEERD = ("meta", "schemas")
SUBATTRIBUTEN = ("value", "display", "$ref", "type")

_VERGELIJKING = re.compile(
    r'\s*(?P<attr>[A-Za-z][A-Za-z0-9_.:-]*)\s+(?P<op>[A-Za-z]+)\s+(?P<waarde>"(?:[^"\\]|\\.)*")\s*'
)
_EN = re.compile(r"and\s+", re.IGNORECASE)
_LEDENPAD = re.compile(r'^members\[value eq ("(?:[^"\\]|\\.)*")\]$', re.IGNORECASE)


class ScimFout(Exception):
    """Fout in SCIM-formaat (RFC 7644 §3.12)."""

    def __init__(self, status: int, detail: str, scim_type: str | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.scim_type = scim_type

    def resource(self) -> dict[str, Any]:
        """De foutresource."""
        fout: dict[str, Any] = {"schemas": [URN_ERROR], "status": str(self.status)}
        if self.scim_type:
            fout["scimType"] = self.scim_type
        fout["detail"] = self.detail
        return fout


@dataclass(frozen=True)
class Profiel:
    """Wat een resourcetype in dit profiel kent.

    Attributes:
        urn: Kernschema; `<urn>:attr` is gelijk aan `attr`.
        schrijfbaar: Kernattributen die een client mag zetten.
        readonly: Kernattributen die een client niet mag wijzigen (RFC 7644 §3.5.2).
        verplicht: Kernattributen die niet onbepaald mogen worden.
        extensie: Of de agentextensie bij dit type hoort.
    """

    urn: str
    schrijfbaar: tuple[str, ...]
    readonly: tuple[str, ...]
    verplicht: tuple[str, ...]
    extensie: bool


# `active` is in dit profiel altijd bepaald: bij aanmaak standaard true, daarna alleen te zetten.
PROFIELEN = {
    "User": Profiel(URN_USER, USER_ATTRIBUTEN, ("id", "groups"), ("userName", "active"), True),
    "Group": Profiel(URN_GROUP, GROUP_ATTRIBUTEN, ("id",), ("displayName",), False),
}


# Filter --------------------------------------------------------------------------------------


def parse_filter(tekst: str) -> list[tuple[str, str]]:
    """Ontleed `attr eq "waarde"`, eventueel met `and`, tot (attribuut, waarde)-paren.

    Operator en `and` zijn hoofdletterongevoelig; de waarde is een JSON-string (RFC 7644
    §3.4.2.2) en mag zelf spaties, `and` of escapes bevatten.

    Raises:
        ScimFout: `400 invalidFilter` voor alles buiten het profiel.
    """
    paren = []
    positie = 0
    while True:
        gevonden = _VERGELIJKING.match(tekst, positie)
        if gevonden is None:
            raise _filterfout(tekst[positie:])
        paren.append(_vergelijking(gevonden))
        positie = gevonden.end()
        if positie == len(tekst):
            return paren
        en = _EN.match(tekst, positie)
        if en is None:
            raise _filterfout(tekst[positie:])
        positie = en.end()


def _filterfout(rest: str) -> ScimFout:
    return ScimFout(
        400,
        f'filter valt buiten het profiel bij {rest!r}: alleen attr eq "waarde", met and',
        "invalidFilter",
    )


def _vergelijking(gevonden: re.Match[str]) -> tuple[str, str]:
    attribuut, op = gevonden.group("attr"), gevonden.group("op")
    if op.lower() != "eq":
        raise ScimFout(400, f"operator {op!r} wordt niet ondersteund; alleen eq", "invalidFilter")
    naam = canonieke_naam(zonder_kernprefix(attribuut), FILTERBAAR)
    if naam not in FILTERBAAR:
        raise ScimFout(400, f"filteren op {attribuut!r} wordt niet ondersteund", "invalidFilter")
    try:
        waarde = json.loads(gevonden.group("waarde"))
    except ValueError as fout:
        raise ScimFout(400, f"ongeldige string in filter: {fout}", "invalidFilter") from fout
    return naam, waarde


def voldoet(resource: dict[str, Any], paren: list[tuple[str, str]]) -> bool:
    """Of een resource aan alle filterparen voldoet; `caseExact`-attributen exact vergeleken.

    Hoofdletterongevoelig vergelijken gebeurt met `lower()`, net als de uniciteitssleutel van
    `userName` en `displayName` in de opslag.
    """
    return all(_gelijk(attribuut, resource.get(attribuut), waarde) for attribuut, waarde in paren)


def _gelijk(attribuut: str, huidig: Any, waarde: str) -> bool:
    if not isinstance(huidig, str):
        return False
    if attribuut in CASE_EXACT:
        return huidig == waarde
    return huidig.lower() == waarde.lower()


# Attribuutnamen ------------------------------------------------------------------------------


def canonieke_naam(naam: str, bekend: tuple[str, ...]) -> str:
    """De canonieke schrijfwijze van een bekend attribuut; een onbekende naam blijft gelijk."""
    for kandidaat in bekend:
        if kandidaat.lower() == naam.lower():
            return kandidaat
    return naam


def normaliseer_namen(waarden: dict[str, Any], bekend: tuple[str, ...]) -> dict[str, Any]:
    """Zet de sleutels van een object in canonieke schrijfwijze.

    Raises:
        ScimFout: `400 invalidValue` als een attribuut in twee schrijfwijzen voorkomt.
    """
    uit: dict[str, Any] = {}
    for sleutel, waarde in waarden.items():
        naam = canonieke_naam(sleutel, bekend)
        if naam in uit:
            raise ScimFout(400, f"attribuut {naam!r} komt dubbel voor", "invalidValue")
        uit[naam] = waarde
    return uit


def zonder_kernprefix(naam: str) -> str:
    """`urn:ietf:params:scim:schemas:core:2.0:User:userName` wordt `userName`."""
    for urn in (URN_USER, URN_GROUP):
        if naam.lower().startswith(urn.lower() + ":"):
            return naam[len(urn) + 1 :]
    return naam


# PATCH ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Operatie:
    """Eén structureel geldige PATCH-operatie (RFC 7644 §3.5.2)."""

    op: str
    pad: str | None
    waarde: Any


def patchoperaties(body: dict[str, Any]) -> list[Operatie]:
    """Controleer de structuur van een PatchOp-bericht en geef de operaties.

    Raises:
        ScimFout: `400 invalidSyntax` bij een ongeldige berichtstructuur, `400 invalidPath` bij
            een `path` dat geen tekst is, `400 invalidValue` bij `add` zonder waarde.
    """
    bericht = normaliseer_namen(body, ("schemas", "Operations"))
    schemas = bericht.get("schemas")
    if not isinstance(schemas, list) or URN_PATCH.lower() not in {
        s.lower() for s in schemas if isinstance(s, str)
    }:
        raise ScimFout(400, f"schemas moet een lijst met {URN_PATCH} zijn", "invalidSyntax")
    operaties = bericht.get("Operations")
    if not isinstance(operaties, list) or not operaties:
        raise ScimFout(400, "Operations moet een niet-lege lijst zijn", "invalidSyntax")
    return [_operatie(ruw) for ruw in operaties]


def _operatie(ruw: Any) -> Operatie:
    if not isinstance(ruw, dict):
        raise ScimFout(400, "elke operatie moet een object zijn", "invalidSyntax")
    operatie = normaliseer_namen(ruw, ("op", "path", "value"))
    op = operatie.get("op")
    if not isinstance(op, str) or op.lower() not in ("add", "remove", "replace"):
        raise ScimFout(400, f"op moet add, remove of replace zijn, niet {op!r}", "invalidSyntax")
    op = op.lower()
    if op != "remove" and "value" not in operatie:
        raise ScimFout(400, f"{op} vereist value", "invalidSyntax")
    if op == "add" and operatie["value"] is None:
        raise ScimFout(400, "add vereist een waarde, geen null", "invalidValue")
    pad = operatie.get("path")
    if pad is not None and (not isinstance(pad, str) or not pad.strip()):
        raise ScimFout(400, "path moet een niet-lege tekst zijn", "invalidPath")
    return Operatie(op, pad.strip() if pad is not None else None, operatie.get("value"))


@dataclass(frozen=True)
class Doel:
    """Eén attribuut dat een operatie raakt: in de extensie of in het kernschema."""

    in_extensie: bool
    attr: str
    waarde: Any


def schakelaar_uit_patch(body: dict[str, Any]) -> bool | None:
    """De `active`-waarde die een PatchOp van een User expliciet zet, of None.

    Leest de doelen op dezelfde manier als `pas_patch_toe`, zodat elke schrijfwijze die de
    patch toepast (`active`, `ACTIVE`, `<kernschema>:active`) ook de schakelaar zet.
    """
    profiel = PROFIELEN["User"]
    gezet: bool | None = None
    for operatie in patchoperaties(body):
        for doel in _doelen(operatie, profiel):
            if not doel.in_extensie and doel.attr.lower() == "active":
                gezet = doel.waarde if isinstance(doel.waarde, bool) else gezet
    return gezet


def pas_patch_toe(resource: dict[str, Any], body: dict[str, Any], soort: str) -> dict[str, Any]:
    """Pas een PatchOp toe op een kopie; faalt één operatie, dan verandert niets.

    Args:
        resource: De huidige resource.
        body: Het PatchOp-bericht.
        soort: `User` of `Group`.

    Raises:
        ScimFout: Bij een ongeldig bericht (`invalidSyntax`), een pad buiten het profiel
            (`invalidPath`) of een readOnly of verplicht doel (`mutability`).
    """
    profiel = PROFIELEN[soort]
    nieuw = copy.deepcopy(resource)
    for operatie in patchoperaties(body):
        for doel in _doelen(operatie, profiel):
            _patch_doel(nieuw, operatie.op, doel, profiel)
    return nieuw


def _doelen(operatie: Operatie, profiel: Profiel) -> list[Doel]:
    """De attributen die één operatie raakt, met of zonder `path`."""
    if operatie.pad is not None:
        return [Doel(*_splits_pad(operatie.pad, profiel), operatie.waarde)]
    if operatie.op == "remove":
        raise ScimFout(400, "remove vereist een path", "noTarget")
    if not isinstance(operatie.waarde, dict):
        raise ScimFout(400, "add/replace zonder path verwacht een object", "invalidValue")
    return _doelen_zonder_pad(operatie.waarde, profiel)


def _doelen_zonder_pad(waarde: dict[str, Any], profiel: Profiel) -> list[Doel]:
    """Doelen van een waardeobject (RFC 7644 §3.5.2.1 en §3.5.2.3).

    Een sleutel is een attribuut (`active`), een schema-gekwalificeerd attribuut
    (`<kernschema>:active`, `<extensie>:doel`) of de extensie als object.

    Raises:
        ScimFout: `400 invalidValue` als één attribuut in twee schrijfwijzen voorkomt.
    """
    doelen: dict[tuple[bool, str], Doel] = {}
    for sleutel, inhoud in normaliseer_namen(waarde, KERN).items():
        for doel in _doelen_van_sleutel(sleutel, inhoud, profiel):
            index = (doel.in_extensie, doel.attr.lower())
            if index in doelen:
                raise ScimFout(400, f"attribuut {doel.attr!r} komt dubbel voor", "invalidValue")
            doelen[index] = doel
    return list(doelen.values())


def _doelen_van_sleutel(sleutel: str, inhoud: Any, profiel: Profiel) -> list[Doel]:
    if not (profiel.extensie and sleutel == URN_AGENT):
        return [Doel(*_splits_pad(sleutel, profiel), inhoud)]
    if not isinstance(inhoud, dict):
        raise ScimFout(400, f"{URN_AGENT} verwacht een object", "invalidValue")
    return [Doel(True, sub, subwaarde) for sub, subwaarde in inhoud.items()]


def _splits_pad(pad: str, profiel: Profiel) -> tuple[bool, str]:
    """(in de extensie, attribuut) voor `attr`, `<kernschema>:attr` of `<extensie>:attr`."""
    lager = pad.lower()
    if profiel.extensie and lager.startswith(URN_AGENT.lower() + ":"):
        return True, pad[len(URN_AGENT) + 1 :]
    if lager.startswith(profiel.urn.lower() + ":"):
        return False, pad[len(profiel.urn) + 1 :]
    if lager.startswith("urn:"):
        raise ScimFout(400, f"onbekend schema in path: {pad}", "invalidPath")
    return False, pad


def _patch_doel(resource: dict[str, Any], op: str, doel: Doel, profiel: Profiel) -> None:
    attr = doel.attr
    if doel.in_extensie:
        _patch_extensie(resource, op, attr, doel.waarde)
    elif "members" in profiel.schrijfbaar and (attr.lower() == "members" or _LEDENPAD.match(attr)):
        _patch_leden(resource, op, attr, doel.waarde)
    else:
        _patch_kern(resource, Operatie(op, attr, doel.waarde), attr, profiel)


def _alleen_topniveau(attr: str) -> None:
    if "[" in attr or "." in attr:
        raise ScimFout(
            400, f"path {attr!r} valt buiten het profiel: alleen hele attributen", "invalidPath"
        )


def _patch_kern(resource: dict[str, Any], operatie: Operatie, attr: str, profiel: Profiel) -> None:
    _alleen_topniveau(attr)
    naam = canonieke_naam(attr, (*profiel.schrijfbaar, *profiel.readonly, *GENEGEERD))
    if naam in GENEGEERD:
        return
    if naam in profiel.readonly:
        _eis_ongewijzigd(resource, operatie.op, naam, operatie.waarde)
    elif naam in profiel.schrijfbaar and naam != URN_AGENT:
        _zet(resource, operatie.op, naam, operatie.waarde, profiel.verplicht)
    else:
        raise ScimFout(400, f"attribuut {attr!r} bestaat niet in dit profiel", "invalidPath")


def _patch_extensie(resource: dict[str, Any], op: str, attr: str, waarde: Any) -> None:
    _alleen_topniveau(attr)
    if not is_agent(resource):
        raise ScimFout(400, "de agentextensie bestaat alleen bij userType agent", "invalidPath")
    ext = resource.setdefault(URN_AGENT, {})
    naam = canonieke_naam(attr, AGENT_ATTRIBUTEN)
    if naam in AGENT_READONLY:
        _eis_ongewijzigd(ext, op, naam, waarde)
    elif naam in AGENT_SCHRIJFBAAR:
        _zet(ext, op, naam, waarde, AGENT_VERPLICHT)
    else:
        raise ScimFout(400, f"extensieattribuut {attr!r} bestaat niet", "invalidPath")


def _eis_ongewijzigd(doel: dict[str, Any], op: str, naam: str, waarde: Any) -> None:
    """ReadOnly: alleen de huidige waarde mag terugkomen (RFC 7644 §3.5.2 en §3.5.2.2)."""
    if op == "remove" or waarde is None or doel.get(naam) != waarde:
        raise ScimFout(400, f"{naam} is readOnly en kan niet worden gewijzigd", "mutability")


def _zet(doel: dict[str, Any], op: str, naam: str, waarde: Any, verplicht: tuple[str, ...]) -> None:
    """Pas één operatie toe; null is onbepaald (RFC 7643 §2.5), ook bij `replace`."""
    if op == "remove" or waarde is None:
        if naam in verplicht:
            raise ScimFout(400, f"{naam} is verplicht en kan niet weg", "mutability")
        doel.pop(naam, None)
    elif op == "add" and isinstance(doel.get(naam), list) and isinstance(waarde, list):
        doel[naam] = doel[naam] + [w for w in waarde if w not in doel[naam]]
    else:
        doel[naam] = waarde


def _patch_leden(resource: dict[str, Any], op: str, pad: str, waarde: Any) -> None:
    """`members` en `members[value eq "id"]`; een niet-lid verwijderen is een succes."""
    leden: list[dict[str, Any]] = list(resource.get("members") or [])
    gefilterd = _LEDENPAD.match(pad)
    if gefilterd:
        if op != "remove":
            raise ScimFout(400, "alleen remove op members[value eq ...]", "invalidPath")
        try:
            weg = {json.loads(gefilterd.group(1))}
        except ValueError as fout:
            raise ScimFout(400, f"ongeldige string in path: {fout}", "invalidPath") from fout
    elif op == "remove" or waarde is None:
        weg = None if waarde is None else set(_leden_ids(waarde))
    else:
        nieuw = list(dict.fromkeys(_leden_ids(waarde)))
        bestaand = [] if op == "replace" else [m["value"] for m in leden]
        resource["members"] = [{"value": v} for v in dict.fromkeys(bestaand + nieuw)]
        return
    resource["members"] = [] if weg is None else [m for m in leden if m.get("value") not in weg]


def _leden_ids(waarde: Any) -> list[str]:
    """De `value` van elk member-object; subattribuutnamen hoofdletterongevoelig."""
    if not isinstance(waarde, list) or not all(isinstance(m, dict) for m in waarde):
        raise ScimFout(400, "members verwacht een lijst van {value}-objecten", "invalidValue")
    ids = [normaliseer_namen(lid, SUBATTRIBUTEN).get("value") for lid in waarde]
    if not all(isinstance(i, str) and i for i in ids):
        raise ScimFout(400, "elk member vereist een value", "invalidValue")
    return [str(i) for i in ids]


# Validatie -----------------------------------------------------------------------------------


def is_agent(resource: dict[str, Any]) -> bool:
    """Of een User-resource een agent is (`userType` hoofdletterongevoelig `agent`)."""
    soort = resource.get("userType")
    return isinstance(soort, str) and soort.lower() == "agent"


def strip_server(resource: dict[str, Any], readonly: tuple[str, ...]) -> dict[str, Any]:
    """Verwijder server- en readOnly-attributen uit een clientbody (RFC 7644 §3.3, §3.5.1)."""
    schoon = {k: v for k, v in normaliseer_namen(resource, KERN).items() if k not in readonly}
    ext = schoon.get(URN_AGENT)
    if isinstance(ext, dict):
        ext = normaliseer_namen(ext, AGENT_ATTRIBUTEN)
        schoon[URN_AGENT] = {k: v for k, v in ext.items() if k not in AGENT_READONLY}
    return schoon


def _zonder_null(waarden: dict[str, Any]) -> dict[str, Any]:
    """Null is gelijk aan niet toegekend (RFC 7643 §2.5)."""
    return {k: v for k, v in waarden.items() if v is not None}


def _eis_bekend(waarden: dict[str, Any], bekend: tuple[str, ...], wat: str) -> None:
    onbekend = sorted(set(waarden) - set(bekend))
    if onbekend:
        raise ScimFout(400, f"{wat} buiten het profiel: {onbekend}", "invalidValue")


def _eis_tekst(waarden: dict[str, Any], *namen: str) -> None:
    for naam in namen:
        if naam in waarden and not isinstance(waarden[naam], str):
            raise ScimFout(400, f"{naam} moet een string zijn", "invalidValue")


def _verplichte_tekst(waarden: dict[str, Any], naam: str) -> str:
    waarde = waarden.get(naam)
    if not isinstance(waarde, str) or not waarde.strip():
        raise ScimFout(400, f"{naam} is verplicht", "invalidValue")
    return waarde.strip()


def valideer_user(resource: dict[str, Any]) -> dict[str, Any]:
    """Controleer een User (mens of agent) en normaliseer; geeft de genormaliseerde resource.

    Verwacht de uitkomst van `strip_server`: canonieke namen, zonder server-attributen.

    Raises:
        ScimFout: `400 invalidValue` bij ontbrekende, onbekende of ongeldige attributen.
    """
    resource = _zonder_null(resource)
    _eis_bekend(resource, USER_ATTRIBUTEN, "attributen")
    _eis_tekst(resource, "displayName", "userType", "externalId")
    resource["userName"] = _verplichte_tekst(resource, "userName")
    resource.setdefault("active", True)
    if not isinstance(resource["active"], bool):
        raise ScimFout(400, "active moet een boolean zijn", "invalidValue")
    if is_agent(resource):
        resource["userType"] = "agent"
        resource[URN_AGENT] = _valideer_agentextensie(resource.get(URN_AGENT))
        resource["schemas"] = [URN_USER, URN_AGENT]
    elif URN_AGENT in resource:
        raise ScimFout(
            400, "de agentextensie is alleen toegestaan bij userType agent", "invalidValue"
        )
    else:
        resource["schemas"] = [URN_USER]
    return resource


def _valideer_agentextensie(ruw: Any) -> dict[str, Any]:
    if not isinstance(ruw, dict):
        raise ScimFout(400, f"een agent vereist de extensie {URN_AGENT}", "invalidValue")
    ext = _zonder_null(ruw)
    _eis_bekend(ext, AGENT_ATTRIBUTEN, "extensieattributen")
    ext["eigenaar"] = {"value": _eigenaar_id(ext.get("eigenaar"))}
    for verplicht in ("doel", "vervaldatum", "publiekeSleutel"):
        _verplichte_tekst(ext, verplicht)
    _eis_tekst(ext, "herbeoordelingTermijn", "bevestigingstermijn")
    if ext.get("risicoklasse") not in RISICOKLASSEN:
        raise ScimFout(400, f"risicoklasse moet een van {RISICOKLASSEN} zijn", "invalidValue")
    try:
        parse_tijd(ext["vervaldatum"])
        controleer_jwk(ext["publiekeSleutel"])
        for termijn in ("herbeoordelingTermijn", "bevestigingstermijn"):
            if termijn in ext:
                parse_duur(ext[termijn])
    except ValueError as fout:
        raise ScimFout(400, str(fout), "invalidValue") from fout
    tools = ext.get("toegestaneTools", [])
    if not isinstance(tools, list) or not all(isinstance(t, str) and t for t in tools):
        raise ScimFout(400, "toegestaneTools moet een lijst van namen zijn", "invalidValue")
    ext["toegestaneTools"] = sorted(set(tools))
    return ext


def _eigenaar_id(eigenaar: Any) -> str:
    if isinstance(eigenaar, dict):
        waarde = normaliseer_namen(eigenaar, SUBATTRIBUTEN).get("value")
        if isinstance(waarde, str) and waarde:
            return waarde
    raise ScimFout(400, "eigenaar.value (SCIM-id van de eigenaar) is verplicht", "invalidValue")


def valideer_group(resource: dict[str, Any]) -> dict[str, Any]:
    """Controleer een Group en normaliseer de leden tot `{value}`.

    Raises:
        ScimFout: `400 invalidValue` bij ontbrekende, onbekende of ongeldige attributen.
    """
    resource = _zonder_null(resource)
    _eis_bekend(resource, GROUP_ATTRIBUTEN, "attributen")
    _eis_tekst(resource, "externalId")
    resource["displayName"] = _verplichte_tekst(resource, "displayName")
    leden = dict.fromkeys(_leden_ids(resource.get("members", [])))
    resource["members"] = [{"value": v} for v in leden]
    resource["schemas"] = [URN_GROUP]
    return resource


def leden_van(groep: dict[str, Any]) -> set[str]:
    """Id's van de leden van een groep."""
    return {str(m.get("value")) for m in groep.get("members") or []}


# Discovery -----------------------------------------------------------------------------------


def _attr(naam: str, typ: str = "string", **extra: Any) -> dict[str, Any]:
    basis: dict[str, Any] = {
        "name": naam,
        "type": typ,
        "multiValued": False,
        "required": False,
        "caseExact": False,
        "mutability": "readWrite",
        "returned": "default",
        "uniqueness": "none",
    }
    basis.update(extra)
    return basis


def _ref(soort: str) -> dict[str, Any]:
    return _attr("$ref", "reference", mutability="readOnly", referenceTypes=[soort])


def _schemameta(basis: str, urn: str) -> dict[str, str]:
    return {"resourceType": "Schema", "location": f"{basis}/scim/v2/Schemas/{urn}"}


def agentschema(basis: str) -> dict[str, Any]:
    """Het extensieschema als SCIM-Schema-resource; `basis` is de externe basis-URL."""
    readonly = {"mutability": "readOnly"}
    return {
        "schemas": [URN_SCHEMA],
        "id": URN_AGENT,
        "name": "Agent",
        "description": "Levenscyclus en eigenaarschap van een AI-agent als identiteit",
        "attributes": [
            _attr(
                "eigenaar",
                "complex",
                required=True,
                subAttributes=[
                    _attr("value", required=True, caseExact=True),
                    _ref("User"),
                    _attr("display", mutability="readOnly"),
                ],
            ),
            _attr("doel", required=True),
            _attr("vervaldatum", "dateTime", required=True),
            _attr("risicoklasse", required=True, canonicalValues=list(RISICOKLASSEN)),
            _attr("toegestaneTools", multiValued=True),
            _attr("publiekeSleutel", required=True, caseExact=True),
            _attr("herbeoordelingTermijn"),
            _attr("bevestigingstermijn"),
            *[
                _attr(naam, "dateTime", **readonly)
                for naam in AGENT_READONLY
                if naam.endswith(("Op", "Sinds", "Bevestiging", "Herbeoordeling", "deadline"))
            ],
            _attr("levenscyclusStatus", **readonly),
            _attr("statusReden", **readonly),
            _attr("bevestigdeVersie", **readonly),
        ],
        "meta": _schemameta(basis, URN_AGENT),
    }


def kernschemas(basis: str) -> list[dict[str, Any]]:
    """Core User en Group, beperkt tot de attributen die dit profiel gebruikt."""
    user = {
        "schemas": [URN_SCHEMA],
        "id": URN_USER,
        "name": "User",
        "attributes": [
            _attr("userName", required=True, uniqueness="server"),
            _attr("displayName"),
            _attr("userType"),
            _attr("externalId", caseExact=True),
            _attr("active", "boolean"),
            _attr(
                "groups",
                "complex",
                multiValued=True,
                mutability="readOnly",
                subAttributes=[
                    _attr("value", mutability="readOnly", caseExact=True),
                    _ref("Group"),
                    _attr("display", mutability="readOnly"),
                ],
            ),
        ],
        "meta": _schemameta(basis, URN_USER),
    }
    group = {
        "schemas": [URN_SCHEMA],
        "id": URN_GROUP,
        "name": "Group",
        "attributes": [
            _attr("displayName", required=True, uniqueness="server"),
            _attr("externalId", caseExact=True),
            _attr(
                "members",
                "complex",
                multiValued=True,
                subAttributes=[_attr("value", caseExact=True, mutability="immutable")],
            ),
        ],
        "meta": _schemameta(basis, URN_GROUP),
    }
    return [user, group]


def lijstantwoord(resources: list[dict[str, Any]]) -> dict[str, Any]:
    """Een volledige ListResponse zonder paginering (RFC 7644 §3.4.2, §4)."""
    return {
        "schemas": [URN_LIST],
        "totalResults": len(resources),
        "itemsPerPage": len(resources),
        "startIndex": 1,
        "Resources": resources,
    }


def resource_types(basis: str) -> list[dict[str, Any]]:
    """ResourceTypes met de extensie op User; `basis` is de externe basis-URL."""
    locatie = f"{basis}/scim/v2/ResourceTypes"
    return [
        {
            "schemas": [URN_RT],
            "id": "User",
            "name": "User",
            "endpoint": "/Users",
            "schema": URN_USER,
            "schemaExtensions": [{"schema": URN_AGENT, "required": False}],
            "meta": {"resourceType": "ResourceType", "location": f"{locatie}/User"},
        },
        {
            "schemas": [URN_RT],
            "id": "Group",
            "name": "Group",
            "endpoint": "/Groups",
            "schema": URN_GROUP,
            "meta": {"resourceType": "ResourceType", "location": f"{locatie}/Group"},
        },
    ]


def service_provider_config(basis: str) -> dict[str, Any]:
    """ServiceProviderConfig volgens het profiel; `basis` is de externe basis-URL."""
    return {
        "schemas": [URN_SPC],
        "documentationUri": "https://github.com/oomny-nl/agent-levenscyclus",
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": 200},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": True},
        "authenticationSchemes": [
            {
                "type": "oauthbearertoken",
                "name": "Bearer token",
                "description": (
                    "Lokaal gegenereerd token per principal (bron, beheerder, poort, eigenaar)"
                ),
            }
        ],
        "meta": {
            "resourceType": "ServiceProviderConfig",
            "location": f"{basis}/scim/v2/ServiceProviderConfig",
        },
    }
