"""Herstel van het register: een halve OpenFGA-herbouw en de demoklok na een herstart."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from levenscyclus.fga import FgaIds
from levenscyclus.klok import Klok
from levenscyclus.register.app import demoklok_uit_logboek
from levenscyclus.register.opslag import Opslag
from tests.unit.conftest import Register, instellingen


async def test_halve_herbouw_wordt_hervat(
    register: Register, monkeypatch: pytest.MonkeyPatch
) -> None:
    eigenaar = await register.mens("sanne")
    await register.team("werk", eigenaar)
    agent = await register.agent(eigenaar)
    assert (await register.bevestig(agent)).status_code == 200
    # Storeverlies: nieuwe id's, lege store, en de eerste herbouw faalt halverwege.
    register.fga.tuples.clear()

    async def nieuwe_ids() -> FgaIds:
        return FgaIds("store-2", "model-2")

    monkeypatch.setattr(register.dienst, "_zorg_ids", nieuwe_ids)
    register.fga.faal = True
    assert (await register.verzoek("GET", "/openfga", "poort")).status_code == 503
    register.fga.faal = False
    # Dezelfde id's als bij de mislukte poging: de herbouw moet toch alsnog draaien.
    assert (await register.verzoek("GET", "/openfga", "poort")).status_code == 200
    assert any(t.user == f"agent:{agent}" for t in register.fga.tuples)
    assert not register.dienst.herbouw_open


async def test_demoklok_overleeft_herstart(register: Register, tmp_path: Path) -> None:
    for stap in ("P31D", "P60D"):
        antwoord = await register.verzoek("POST", "/beheer/klok", "beheerder", {"vooruit": stap})
        assert antwoord.status_code == 200, antwoord.text
    inst = instellingen(tmp_path)
    herstart = Opslag(inst.db_pad, inst.anker_pad, inst.register_url)
    herstart.verifieer_bij_start()
    klok = Klok(demostand=True)
    klok.herstel(demoklok_uit_logboek(herstart.logboek()))
    assert klok.offset == timedelta(days=91)
    with pytest.raises(PermissionError, match="DEMO_KLOK"):
        Klok(demostand=False).herstel(klok.offset)
