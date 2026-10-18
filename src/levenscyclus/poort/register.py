"""Client van de poort op het register: agentrecords lezen, logboek schrijven, OpenFGA-id's."""

from __future__ import annotations

import json
import uuid
from typing import Any
from urllib.parse import quote

import httpx2

from levenscyclus.register.scim import URN_AGENT


class RegisterFout(Exception):
    """Het register is onbereikbaar of gaf een onverwachte fout; de poort faalt dicht."""


def _is_register_id(tekst: str) -> bool:
    """Of `tekst` een SCIM-id is zoals het register die maakt (`str(uuid.uuid4())`)."""
    try:
        return str(uuid.UUID(tekst)) == tekst
    except ValueError:
        return False


class Register:
    """HTTP-client met het `poort`-token.

    Args:
        url: Basis-URL van het register.
        token: Het `poort`-token uit de geheimenmap.
    """

    def __init__(self, url: str, token: str) -> None:
        self._url = url.rstrip("/")
        self._http = httpx2.AsyncClient(
            base_url=self._url, headers={"Authorization": f"Bearer {token}"}, timeout=10
        )

    async def _get(self, pad: str, **params: str) -> httpx2.Response:
        try:
            return await self._http.get(pad, params=params or None)
        except httpx2.HTTPError as fout:
            raise RegisterFout(f"register onbereikbaar: {fout}") from fout

    async def haal_agent(self, id_: str) -> dict[str, Any] | None:
        """Het SCIM-record van een agent met effectieve status, of None (onbekend of geen agent).

        `id_` komt uit een nog niet geverifieerde assertie. Alleen de vorm die het register uitgeeft
        (een UUID in canonieke schrijfwijze) leidt tot een verzoek, als één gecodeerd padsegment.

        Raises:
            RegisterFout: Bij een fout van het register of een record met een andere `id`.
        """
        if not _is_register_id(id_):
            return None
        antwoord = await self._get(f"/scim/v2/Users/{quote(id_, safe='')}")
        if antwoord.status_code == 404:
            return None
        if antwoord.status_code != 200:
            raise RegisterFout(f"register gaf {antwoord.status_code} bij het lezen van de agent")
        record = antwoord.json()
        if record.get("id") != id_:
            raise RegisterFout(f"register gaf record {record.get('id')!r} voor id {id_!r}")
        return record if URN_AGENT in record and record.get("userType") == "agent" else None

    async def team_id(self, naam: str) -> str | None:
        """SCIM-id van de groep met deze `displayName`, of None als die groep niet bestaat.

        Eén gefilterd verzoek, dus onafhankelijk van het aantal groepen en de paginering.
        `displayName` is uniek en niet hoofdlettergevoelig (RFC 7643 §4.2, `uniqueness` server).

        Raises:
            RegisterFout: Bij een fout van het register, of als het meer dan één groep of een
                groep met een andere naam teruggeeft.
        """
        antwoord = await self._get(
            "/scim/v2/Groups",
            filter=f"displayName eq {json.dumps(naam)}",
            excludedAttributes="members",
        )
        if antwoord.status_code != 200:
            raise RegisterFout(f"register gaf {antwoord.status_code} bij het zoeken van {naam!r}")
        gevonden = antwoord.json().get("Resources", [])
        if not gevonden:
            return None
        if len(gevonden) > 1 or gevonden[0].get("displayName", "").casefold() != naam.casefold():
            raise RegisterFout(f"register gaf geen eenduidige groep voor {naam!r}")
        return str(gevonden[0]["id"])

    async def openfga_ids(self) -> tuple[str, str]:
        """Store- en model-id; het register herstelt store en model als ze ontbreken."""
        antwoord = await self._get("/openfga")
        if antwoord.status_code != 200:
            raise RegisterFout(f"register gaf {antwoord.status_code} bij /openfga")
        gegevens = antwoord.json()
        return gegevens["store_id"], gegevens["model_id"]

    async def log(self, record: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Schrijf een toolcall-record; (201, record) of (409, record als geweigerd)."""
        try:
            antwoord = await self._http.post("/logboek", json=record)
        except httpx2.HTTPError as fout:
            raise RegisterFout(f"logboek onbereikbaar: {fout}") from fout
        if antwoord.status_code not in (201, 409):
            raise RegisterFout(f"logboek gaf {antwoord.status_code}: {antwoord.text[:200]}")
        return antwoord.status_code, antwoord.json()

    async def is_beheerder(self, authorization: str) -> bool:
        """Of dit token het beheerderstoken is (het register beslist)."""
        try:
            antwoord = await self._http.get(
                "/beheer/agents", headers={"Authorization": authorization}
            )
        except httpx2.HTTPError as fout:
            raise RegisterFout(f"register onbereikbaar: {fout}") from fout
        return antwoord.status_code == 200
