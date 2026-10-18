"""Grenzen van duren en tijdstippen: buiten het bereik een ValueError (dus `400`), nooit een 500."""

from __future__ import annotations

from datetime import timedelta

import pytest

from levenscyclus.duur import MAX_DUUR, parse_duur
from levenscyclus.klok import Klok, parse_tijd


@pytest.mark.parametrize("tekst", ["P" + "9" * 400 + "D", "PT" + "9" * 30 + "S", "P36526D"])
def test_te_lange_duur(tekst: str) -> None:
    with pytest.raises(ValueError, match="maximum"):
        parse_duur(tekst)


def test_maximale_duur_is_honderd_jaar() -> None:
    assert parse_duur("P36525D") == MAX_DUUR == timedelta(days=36525)


def test_tijdstip_buiten_het_datumbereik() -> None:
    with pytest.raises(ValueError, match="datumbereik"):
        parse_tijd("0001-01-01T00:00:00+01:00")


def test_demoklok_staat_maximaal_honderd_jaar_vooruit() -> None:
    klok = Klok(demostand=True)
    klok.zet_vooruit(MAX_DUUR)
    with pytest.raises(ValueError, match="maximaal"):
        klok.zet_vooruit(timedelta(seconds=1))
    assert klok.offset == MAX_DUUR
