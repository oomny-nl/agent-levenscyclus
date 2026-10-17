"""SCIM 2.0-profiel van het register: schema's, filter, PATCH en validatie (RFC 7643/7644).

Bewust beperkt profiel, zie docs/ONTWERP.md §4.1. Alles wat buiten het profiel valt, wordt
herkenbaar geweigerd met een SCIM-foutresource.
"""

from __future__ import annotations

import copy
import re
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
FILTERBAAR = ("username", "displayname", "usertype", "externalid", "id")
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
USER_SERVER = ("id", "meta", "schemas", "groups")
GROUP_SERVER = ("id", "meta", "schemas")

_FILTER_DEEL = re.compile(r'^\s*([A-Za-z][A-Za-z0-9_.:]*)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*$')
_LEDENPAD = re.compile(r'^members\[value eq "((?:[^"\\]|\\.)*)"\]$', re.IGNORECASE)


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


def parse_filter(tekst: str) -> list[tuple[str, str]]:
    """Ontleed `attr eq "waarde"` (met `and`) tot (attribuut in kleine letters, waarde)-paren.

    Raises:
        ScimFout: `400 invalidFilter` voor alles buiten het profiel.
    """
    delen = re.split(r"\s+and\s+", tekst.strip(), flags=re.IGNORECASE)
    paren = []
    for deel in delen:
        gevonden = _FILTER_DEEL.match(deel)
        if gevonden is None:
            raise ScimFout(
                400,
                f'filter {deel!r} valt buiten het profiel: alleen attr eq "waarde", met and',
                "invalidFilter",
            )
        attribuut = gevonden.group(1).lower()
        if attribuut not in FILTERBAAR:
            raise ScimFout(
                400, f"filteren op {gevonden.group(1)!r} wordt niet ondersteund", "invalidFilter"
            )
        paren.append((attribuut, gevonden.group(2).replace('\\"', '"')))
    return paren


def voldoet(resource: dict[str, Any], paren: list[tuple[str, str]]) -> bool:
    """Of een resource aan alle filterparen voldoet (tekstueel, hoofdletterongevoelig)."""
    sleutels = {k.lower(): k for k in resource}
    for attribuut, waarde in paren:
        echt = sleutels.get(attribuut)
        huidig = resource.get(echt) if echt else None
        if huidig is None or str(huidig).lower() != waarde.lower():
            return False
    return True


# PATCH -------------------------------------------------------------------------------------------


def _splits_pad(pad: str) -> tuple[str | None, str]:
    """Splits `urn:...:attr` in (urn, attr); zonder urn is de eerste None."""
    if ":" in pad:
        urn, _, attr = pad.rpartition(":")
        return urn, attr
    return None, pad


def _doel(resource: dict[str, Any], urn: str | None) -> dict[str, Any]:
    if urn is None:
        return resource
    if urn.lower() == URN_AGENT.lower():
        return resource.setdefault(URN_AGENT, {})
    if urn.lower() in (URN_USER.lower(), URN_GROUP.lower()):
        return resource
    raise ScimFout(400, f"onbekend schema in path: {urn}", "invalidPath")


def _patch_leden(resource: dict[str, Any], op: str, pad: str, waarde: Any) -> None:
    """`members`, `members[value eq "id"]`."""
    leden: list[dict[str, Any]] = list(resource.get("members") or [])
    gefilterd = _LEDENPAD.match(pad)
    if gefilterd:
        if op != "remove":
            raise ScimFout(400, "alleen remove op members[value eq ...]", "invalidPath")
        resource["members"] = [m for m in leden if m.get("value") != gefilterd.group(1)]
        return
    if op == "remove":
        if waarde is None:
            resource["members"] = []
        else:
            weg = {m.get("value") for m in _lijst(waarde)}
            resource["members"] = [m for m in leden if m.get("value") not in weg]
        return
    nieuw = [{"value": str(m.get("value"))} for m in _lijst(waarde)]
    if op == "replace":
        resource["members"] = nieuw
        return
    bestaand = {m.get("value") for m in leden}
    resource["members"] = leden + [m for m in nieuw if m["value"] not in bestaand]


def _lijst(waarde: Any) -> list[dict[str, Any]]:
    if not isinstance(waarde, list) or not all(isinstance(m, dict) for m in waarde):
        raise ScimFout(400, "members verwacht een lijst van {value}-objecten", "invalidValue")
    return waarde


def _patch_attribuut(resource: dict[str, Any], op: str, pad: str, waarde: Any) -> None:
    urn, attr = _splits_pad(pad)
    if attr.lower() == "members" or _LEDENPAD.match(attr):
        _patch_leden(resource, op, attr, waarde)
        return
    if "[" in attr or "." in attr:
        raise ScimFout(400, f"path {pad!r} valt buiten het profiel", "invalidPath")
    doel = _doel(resource, urn)
    if op == "remove":
        doel.pop(attr, None)
    elif op == "add" and isinstance(doel.get(attr), list) and isinstance(waarde, list):
        doel[attr] = doel[attr] + [w for w in waarde if w not in doel[attr]]
    else:
        doel[attr] = waarde


def _patch_zonder_pad(resource: dict[str, Any], op: str, waarde: Any) -> None:
    if op == "remove":
        raise ScimFout(400, "remove vereist een path", "noTarget")
    if not isinstance(waarde, dict):
        raise ScimFout(400, "add/replace zonder path verwacht een object", "invalidValue")
    for sleutel, inhoud in waarde.items():
        if sleutel.lower() == URN_AGENT.lower() and isinstance(inhoud, dict):
            for sub, subwaarde in inhoud.items():
                _patch_attribuut(resource, op, f"{URN_AGENT}:{sub}", subwaarde)
        else:
            _patch_attribuut(resource, op, sleutel, inhoud)


def pas_patch_toe(resource: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """Pas een PatchOp toe op een kopie; faalt één operatie, dan verandert niets.

    Raises:
        ScimFout: Bij een ongeldig PatchOp-bericht of een pad buiten het profiel.
    """
    if URN_PATCH not in body.get("schemas", []):
        raise ScimFout(400, f"schemas moet {URN_PATCH} bevatten", "invalidValue")
    operaties = body.get("Operations")
    if not isinstance(operaties, list) or not operaties:
        raise ScimFout(400, "Operations ontbreekt of is leeg", "invalidValue")
    nieuw = copy.deepcopy(resource)
    for operatie in operaties:
        op = str(operatie.get("op", "")).lower()
        if op not in ("add", "remove", "replace"):
            raise ScimFout(400, f"onbekende op {operatie.get('op')!r}", "invalidValue")
        pad = operatie.get("path")
        if pad is None:
            _patch_zonder_pad(nieuw, op, operatie.get("value"))
        else:
            _patch_attribuut(nieuw, op, str(pad), operatie.get("value"))
    return nieuw


# Validatie ---------------------------------------------------------------------------------------


def is_agent(resource: dict[str, Any]) -> bool:
    """Of een User-resource een agent is."""
    return str(resource.get("userType", "")).lower() == "agent"


def strip_server(resource: dict[str, Any], readonly: tuple[str, ...]) -> dict[str, Any]:
    """Verwijder server- en readOnly-attributen uit een clientbody (RFC 7644 §3.5.1)."""
    schoon = {k: v for k, v in resource.items() if k not in readonly}
    ext = schoon.get(URN_AGENT)
    if isinstance(ext, dict):
        schoon[URN_AGENT] = {k: v for k, v in ext.items() if k not in AGENT_READONLY}
    return schoon


def valideer_user(resource: dict[str, Any]) -> dict[str, Any]:
    """Controleer een User (mens of agent) en normaliseer; geeft de genormaliseerde resource.

    Raises:
        ScimFout: `400 invalidValue` bij ontbrekende of ongeldige attributen.
    """
    naam = resource.get("userName")
    if not isinstance(naam, str) or not naam.strip():
        raise ScimFout(400, "userName is verplicht", "invalidValue")
    resource["userName"] = naam.strip()
    resource.setdefault("active", True)
    if not isinstance(resource["active"], bool):
        raise ScimFout(400, "active moet een boolean zijn", "invalidValue")
    for sleutel in list(resource):
        if sleutel.lower() == URN_AGENT.lower() and sleutel != URN_AGENT:
            resource[URN_AGENT] = resource.pop(sleutel)
    if is_agent(resource):
        resource[URN_AGENT] = _valideer_agentextensie(resource.get(URN_AGENT))
        resource["schemas"] = [URN_USER, URN_AGENT]
    else:
        if URN_AGENT in resource:
            raise ScimFout(
                400, "de agentextensie is alleen toegestaan bij userType agent", "invalidValue"
            )
        resource["schemas"] = [URN_USER]
    return resource


def _valideer_agentextensie(ext: Any) -> dict[str, Any]:  # noqa: C901
    if not isinstance(ext, dict):
        raise ScimFout(400, f"een agent vereist de extensie {URN_AGENT}", "invalidValue")
    onbekend = set(ext) - set(AGENT_SCHRIJFBAAR) - set(AGENT_READONLY)
    if onbekend:
        raise ScimFout(400, f"onbekende extensieattributen: {sorted(onbekend)}", "invalidValue")
    eigenaar = ext.get("eigenaar")
    if not isinstance(eigenaar, dict) or not isinstance(eigenaar.get("value"), str):
        raise ScimFout(400, "eigenaar.value (SCIM-id van de eigenaar) is verplicht", "invalidValue")
    ext["eigenaar"] = {"value": eigenaar["value"]}
    for verplicht in ("doel", "vervaldatum", "publiekeSleutel"):
        if not isinstance(ext.get(verplicht), str) or not ext[verplicht].strip():
            raise ScimFout(400, f"{verplicht} is verplicht", "invalidValue")
    if ext.get("risicoklasse") not in RISICOKLASSEN:
        raise ScimFout(400, f"risicoklasse moet een van {RISICOKLASSEN} zijn", "invalidValue")
    try:
        parse_tijd(ext["vervaldatum"])
        controleer_jwk(ext["publiekeSleutel"])
        for termijn in ("herbeoordelingTermijn", "bevestigingstermijn"):
            if termijn in ext:
                parse_duur(str(ext[termijn]))
    except ValueError as fout:
        raise ScimFout(400, str(fout), "invalidValue") from fout
    tools = ext.get("toegestaneTools", [])
    if not isinstance(tools, list) or not all(isinstance(t, str) and t for t in tools):
        raise ScimFout(400, "toegestaneTools moet een lijst van namen zijn", "invalidValue")
    ext["toegestaneTools"] = sorted(set(tools))
    return ext


def valideer_group(resource: dict[str, Any]) -> dict[str, Any]:
    """Controleer een Group en normaliseer de leden tot `{value}`."""
    naam = resource.get("displayName")
    if not isinstance(naam, str) or not naam.strip():
        raise ScimFout(400, "displayName is verplicht", "invalidValue")
    resource["displayName"] = naam.strip()
    leden = resource.get("members") or []
    if not isinstance(leden, list):
        raise ScimFout(400, "members moet een lijst zijn", "invalidValue")
    gezien: list[str] = []
    for lid in leden:
        if not isinstance(lid, dict) or not isinstance(lid.get("value"), str):
            raise ScimFout(400, "elk member vereist value", "invalidValue")
        if lid["value"] not in gezien:
            gezien.append(lid["value"])
    resource["members"] = [{"value": v} for v in gezien]
    resource["schemas"] = [URN_GROUP]
    return resource


def leden_van(groep: dict[str, Any]) -> set[str]:
    """Id's van de leden van een groep."""
    return {str(m.get("value")) for m in groep.get("members") or []}


# Discovery ---------------------------------------------------------------------------------------


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


def agentschema() -> dict[str, Any]:
    """Het extensieschema als SCIM-Schema-resource."""
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
                    _attr("value", required=True, mutability="readWrite"),
                    _attr("$ref", "reference", mutability="readOnly", referenceTypes=["User"]),
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
        "meta": {"resourceType": "Schema", "location": f"/scim/v2/Schemas/{URN_AGENT}"},
    }


def kernschemas() -> list[dict[str, Any]]:
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
                    _attr("value", mutability="readOnly"),
                    _attr("display", mutability="readOnly"),
                ],
            ),
        ],
        "meta": {"resourceType": "Schema", "location": f"/scim/v2/Schemas/{URN_USER}"},
    }
    group = {
        "schemas": [URN_SCHEMA],
        "id": URN_GROUP,
        "name": "Group",
        "attributes": [
            _attr("displayName", required=True),
            _attr("externalId", caseExact=True),
            _attr("members", "complex", multiValued=True, subAttributes=[_attr("value")]),
        ],
        "meta": {"resourceType": "Schema", "location": f"/scim/v2/Schemas/{URN_GROUP}"},
    }
    return [user, group]


def resource_types() -> list[dict[str, Any]]:
    """ResourceTypes met de extensie op User."""
    return [
        {
            "schemas": [URN_RT],
            "id": "User",
            "name": "User",
            "endpoint": "/Users",
            "schema": URN_USER,
            "schemaExtensions": [{"schema": URN_AGENT, "required": False}],
            "meta": {"resourceType": "ResourceType", "location": "/scim/v2/ResourceTypes/User"},
        },
        {
            "schemas": [URN_RT],
            "id": "Group",
            "name": "Group",
            "endpoint": "/Groups",
            "schema": URN_GROUP,
            "meta": {"resourceType": "ResourceType", "location": "/scim/v2/ResourceTypes/Group"},
        },
    ]


def service_provider_config() -> dict[str, Any]:
    """ServiceProviderConfig volgens het profiel."""
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
            "location": "/scim/v2/ServiceProviderConfig",
        },
    }
