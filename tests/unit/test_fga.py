"""De OpenFGA-adapter met de echte SDK: elke transportfout wordt `FgaFout` (fail closed)."""

from __future__ import annotations

import socket
from typing import Any

import aiohttp
import pytest

from levenscyclus.fga import Fga, FgaFout, FgaIds, Tuple, zorg_store
from tests.unit.conftest import NepFga

IDS = FgaIds("01ARZ3NDEKTSV4RRFFQ69G5FAV", "01ARZ3NDEKTSV4RRFFQ69G5FAW")


def _gesloten_poort() -> str:
    """URL van een poort waar niemand luistert: de verbinding wordt geweigerd."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        poort = s.getsockname()[1]
    return f"http://127.0.0.1:{poort}"


async def test_geweigerde_verbinding_wordt_fgafout() -> None:
    url = _gesloten_poort()
    fga = Fga(url, "sleutel", IDS)
    with pytest.raises(FgaFout, match="check mislukt"):
        await fga.check("agent:a", "toegestaan", "tool:dossier_lezen")
    with pytest.raises(FgaFout, match="read mislukt"):
        await fga.lees("dossier:2026-0142")
    with pytest.raises(FgaFout, match="read mislukt"):
        await fga.lees_alles()
    with pytest.raises(FgaFout, match="write mislukt"):
        await fga.verwijder({Tuple("gebruiker:x", "lid", "team:t")})
    with pytest.raises(FgaFout, match="store of model"):
        await zorg_store(url, "sleutel", "levenscyclus", {})


@pytest.mark.parametrize(
    "fout", [TimeoutError(), aiohttp.ServerTimeoutError(), aiohttp.ServerDisconnectedError()]
)
async def test_timeout_en_verbroken_verbinding_worden_fgafout(
    monkeypatch: pytest.MonkeyPatch, fout: Exception
) -> None:
    # De HTTP-grens van de SDK: aiohttp zelf.
    async def faal(*_: Any, **__: Any) -> None:
        raise fout

    monkeypatch.setattr(aiohttp.ClientSession, "_request", faal)
    with pytest.raises(FgaFout, match="check mislukt"):
        await Fga("http://openfga.invalid", "sleutel", IDS).check("agent:a", "toegestaan", "tool:x")


async def test_synchronisatie_boven_maxtuplesperwrite_in_porties() -> None:
    fga = NepFga()
    gewenst = {Tuple("gebruiker:x", "lid", f"team:t{i:03}") for i in range(250)}
    assert await fga.synchroniseer_subject("gebruiker:x", "team", gewenst)
    assert fga.tuples == gewenst
    assert await fga.synchroniseer_subject("gebruiker:x", "team", set())
    assert fga.tuples == set()


async def test_nieuwe_conditie_op_dezelfde_tuple_eerst_weg_dan_erbij() -> None:
    # OpenFGA weigert dezelfde sleutel in deletes en writes van één request.
    fga = NepFga()
    fga.tuples = {Tuple("agent:a", "agent", "gebruiker:m", "2026-11-01T00:00:00.000Z")}
    nieuw = {Tuple("agent:a", "agent", "gebruiker:m", "2027-01-01T00:00:00.000Z")}
    assert await fga.synchroniseer_subject("agent:a", "gebruiker", nieuw)
    assert fga.tuples == nieuw
