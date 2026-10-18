"""Dunne laag om de officiële OpenFGA-SDK: checks en gewenste-toestandsynchronisatie van tuples.

De register- en poortprocessen gebruiken deze module; de agent heeft geen route en geen sleutel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp
import httpx2
from openfga_sdk import (
    ClientConfiguration,
    CreateStoreRequest,
    OpenFgaClient,
    ReadRequestTupleKey,
    RelationshipCondition,
)
from openfga_sdk.client.models import (
    ClientCheckRequest,
    ClientTuple,
    ClientWriteRequest,
)
from openfga_sdk.credentials import CredentialConfiguration, Credentials
from openfga_sdk.exceptions import ApiException

CONDITIE = "niet_verlopen"
# De SDK zet een HTTP-foutstatus om in `ApiException`, maar laat verbindingsfouten en time-outs
# van aiohttp ongewijzigd door (`openfga_sdk/rest.py`, `pool_manager.request`). Alle drie
# betekenen: OpenFGA gaf geen bruikbaar antwoord.
SDK_FOUTEN = (ApiException, aiohttp.ClientError, TimeoutError)
# OpenFGA weigert standaard een write met meer dan 100 tuples (`maxTuplesPerWrite`).
MAX_PER_WRITE = 100


class FgaFout(Exception):
    """OpenFGA gaf een fout of is onbereikbaar; de aanroeper faalt dicht."""


@dataclass(frozen=True, order=True)
class Tuple:
    """Eén relatietuple, met optionele conditie.

    Attributes:
        user: Bijvoorbeeld `gebruiker:<id>`.
        relation: Relatienaam.
        object: Bijvoorbeeld `team:<id>`.
        vervaldatum: Alleen bij `gebruiker#agent`: contextwaarde van de conditie (ISO 8601).
    """

    user: str
    relation: str
    object: str
    vervaldatum: str | None = None

    def client_tuple(self) -> ClientTuple:
        """Zet om naar het SDK-type."""
        conditie = None
        if self.vervaldatum is not None:
            conditie = RelationshipCondition(
                name=CONDITIE, context={"vervaldatum": self.vervaldatum}
            )
        return ClientTuple(
            user=self.user, relation=self.relation, object=self.object, condition=conditie
        )


@dataclass(frozen=True)
class FgaIds:
    """Store- en model-id, zoals het register ze heeft aangemaakt of teruggevonden."""

    store_id: str
    model_id: str


def _configuratie(url: str, api_token: str, ids: FgaIds | None) -> ClientConfiguration:
    return ClientConfiguration(
        api_url=url,
        store_id=ids.store_id if ids else None,
        authorization_model_id=ids.model_id if ids else None,
        credentials=Credentials(
            method="api_token", configuration=CredentialConfiguration(api_token=api_token)
        ),
    )


async def zorg_store(url: str, api_token: str, storenaam: str, model: dict[str, Any]) -> FgaIds:
    """Zorg dat de store met deze naam bestaat en een autorisatiemodel heeft; geef de id's terug.

    Het register is eigenaar van store en model (ONTWERP §4.3). Bestaat de store niet (eerste
    start, of OpenFGA is met zijn in-memory datastore herstart), dan wordt hij aangemaakt en het
    model uit `openfga/model.json` geschreven. Een bestaande store zonder model krijgt het model.

    Raises:
        FgaFout: Als OpenFGA onbereikbaar is of een fout geeft.
    """
    try:
        async with OpenFgaClient(_configuratie(url, api_token, None)) as client:
            stores = await client.list_stores()
            kandidaten = [s for s in stores.stores or [] if s.name == storenaam]
            if kandidaten:
                store_id = max(kandidaten, key=lambda s: s.created_at).id
            else:
                aangemaakt = await client.create_store(CreateStoreRequest(name=storenaam))
                store_id = aangemaakt.id
            client.set_store_id(store_id)
            bestaand = await client.read_latest_authorization_model()
            if bestaand.authorization_model is not None:
                return FgaIds(store_id, bestaand.authorization_model.id)
    except SDK_FOUTEN as fout:
        raise FgaFout(f"OpenFGA onbereikbaar of fout bij store of model: {fout}") from fout
    return FgaIds(store_id, await _schrijf_model(url, api_token, store_id, model))


async def _schrijf_model(url: str, api_token: str, store_id: str, model: dict[str, Any]) -> str:
    """Schrijf het model in JSON-vorm (zoals `fga model transform` het levert) naar de store.

    De SDK-modelklassen hebben geen JSON-constructor; de HTTP-API neemt de JSON rechtstreeks.
    """
    try:
        async with httpx2.AsyncClient(timeout=10) as http:
            antwoord = await http.post(
                f"{url}/stores/{store_id}/authorization-models",
                json=model,
                headers={"Authorization": f"Bearer {api_token}"},
            )
            antwoord.raise_for_status()
    except httpx2.HTTPError as fout:
        raise FgaFout(f"autorisatiemodel schrijven mislukt: {fout}") from fout
    return str(antwoord.json()["authorization_model_id"])


class Fga:
    """OpenFGA-toegang voor register en poort.

    Args:
        url: HTTP-API van OpenFGA.
        api_token: Preshared key.
        ids: Store- en model-id.
    """

    def __init__(self, url: str, api_token: str, ids: FgaIds) -> None:
        self._url = url
        self._api_token = api_token
        self.ids = ids
        self._configuratie = _configuratie(url, api_token, ids)

    def vernieuw(self, ids: FgaIds) -> bool:
        """Gebruik nieuwe store- en model-id's (na herstel); True als ze veranderd zijn."""
        veranderd = ids != self.ids
        self.ids = ids
        self._configuratie = _configuratie(self._url, self._api_token, ids)
        return veranderd

    async def check(
        self, user: str, relation: str, obj: str, context: dict[str, Any] | None = None
    ) -> bool:
        """Eén relatievraag; faalt dicht met `FgaFout` bij een fout."""
        verzoek = ClientCheckRequest(user=user, relation=relation, object=obj, context=context)
        try:
            async with OpenFgaClient(self._configuratie) as client:
                antwoord = await client.check(verzoek)
        except SDK_FOUTEN as fout:
            raise FgaFout(f"OpenFGA-check mislukt: {fout}") from fout
        return bool(antwoord.allowed)

    async def lees(self, obj: str, relation: str | None = None) -> set[Tuple]:
        """Alle tuples op een object (en optioneel één relatie)."""
        return await self._lees(ReadRequestTupleKey(object=obj, relation=relation))

    async def _lees(self, sleutel: ReadRequestTupleKey) -> set[Tuple]:
        gevonden: set[Tuple] = set()
        try:
            async with OpenFgaClient(self._configuratie) as client:
                vervolg: str | None = None
                while True:
                    opties: dict[str, Any] = {"page_size": 100}
                    if vervolg:
                        opties["continuation_token"] = vervolg
                    antwoord = await client.read(sleutel, opties)
                    for t in antwoord.tuples or []:
                        gevonden.add(_uit_sdk(t.key))
                    vervolg = antwoord.continuation_token or None
                    if not vervolg:
                        return gevonden
        except SDK_FOUTEN as fout:
            raise FgaFout(f"OpenFGA-read mislukt: {fout}") from fout

    async def lees_per_subject(self, user: str, objecttype: str) -> set[Tuple]:
        """Alle tuples met dit subject op een objecttype, waar het object ook is."""
        return await self._lees(ReadRequestTupleKey(user=user, object=f"{objecttype}:"))

    async def lees_alles(self) -> set[Tuple]:
        """Alle tuples in de store (een read zonder tuple key)."""
        return await self._lees(ReadRequestTupleKey())

    async def synchroniseer(self, obj: str, relation: str, gewenst: set[Tuple]) -> bool:
        """Breng de tuples op `obj#relation` in de gewenste toestand en lees terug.

        Returns:
            True als er iets is veranderd.

        Raises:
            FgaFout: Als schrijven mislukt of het teruglezen niet de gewenste toestand toont.
        """
        huidig = await self.lees(obj, relation)
        verwijderen = huidig - gewenst
        toevoegen = gewenst - huidig
        if verwijderen or toevoegen:
            await self._schrijf(toevoegen, verwijderen)
        if await self.lees(obj, relation) != gewenst:
            raise FgaFout(f"tuples op {obj}#{relation} kloppen na synchronisatie niet")
        return bool(verwijderen or toevoegen)

    async def synchroniseer_subject(self, user: str, objecttype: str, gewenst: set[Tuple]) -> bool:
        """Breng alle tuples van `user` op objecttype `objecttype` in de gewenste toestand.

        Leest per subject, zodat ook relaties op voormalige objecten (oude eigenaar, ingetrokken
        tool, verwijderde groep) worden gevonden en verwijderd (ONTWERP §4.3).
        """
        huidig = await self.lees_per_subject(user, objecttype)
        verwijderen = huidig - gewenst
        toevoegen = gewenst - huidig
        if verwijderen or toevoegen:
            await self._schrijf(toevoegen, verwijderen)
        if await self.lees_per_subject(user, objecttype) != gewenst:
            raise FgaFout(f"tuples van {user} op {objecttype} kloppen na synchronisatie niet")
        return bool(verwijderen or toevoegen)

    async def verwijder(self, tuples: set[Tuple]) -> None:
        """Verwijder deze tuples, in porties die OpenFGA in één write accepteert."""
        geordend = sorted(tuples)
        for begin in range(0, len(geordend), MAX_PER_WRITE):
            await self._schrijf(set(), set(geordend[begin : begin + MAX_PER_WRITE]))

    async def verwijder_alles(self, obj: str) -> None:
        """Verwijder alle tuples op een object (bij `DELETE` van een agent)."""
        huidig = await self.lees(obj)
        if huidig:
            await self._schrijf(set(), huidig)

    async def _schrijf(self, toevoegen: set[Tuple], verwijderen: set[Tuple]) -> None:
        verzoek = ClientWriteRequest(
            writes=[t.client_tuple() for t in sorted(toevoegen)] or None,
            deletes=[t.client_tuple() for t in sorted(verwijderen)] or None,
        )
        try:
            async with OpenFgaClient(self._configuratie) as client:
                await client.write(verzoek)
        except SDK_FOUTEN as fout:
            raise FgaFout(f"OpenFGA-write mislukt: {fout}") from fout


def _uit_sdk(sleutel: Any) -> Tuple:
    vervaldatum = None
    conditie = getattr(sleutel, "condition", None)
    if conditie is not None and conditie.context:
        vervaldatum = str(conditie.context.get("vervaldatum"))
    return Tuple(sleutel.user, sleutel.relation, sleutel.object, vervaldatum)
