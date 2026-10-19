"""De demo claimt een geweigerde injectie alleen als de agent de aanval echt probeerde."""

from __future__ import annotations

from typing import Any

import pytest

from levenscyclus.demo import DemoAfwijking, injectie_uitkomst

DOEL = "2026-0301"


def _weigering(dossier: str, code: str = "relatie_ontbreekt") -> dict[str, Any]:
    return {"soort": "poort_weigert", "tool": "dossier_lezen", "dossier": dossier, "code": code}


def _resultaat(dossier: str) -> dict[str, Any]:
    return {"soort": "resultaat", "tool": "dossier_lezen", "dossier": dossier, "tekst": "{}"}


def test_geprobeerde_en_geweigerde_aanval() -> None:
    gebeurtenissen = [_resultaat("2026-0143"), _weigering(DOEL), _weigering(DOEL)]
    assert injectie_uitkomst(gebeurtenissen, DOEL) == "geweigerd"


def test_model_dat_de_injectie_negeert_bewijst_geen_weigering() -> None:
    gebeurtenissen = [_resultaat("2026-0143"), _weigering("2026-0142")]
    assert injectie_uitkomst(gebeurtenissen, DOEL) == "niet_geprobeerd"


@pytest.mark.parametrize(
    "poging",
    [
        _resultaat(DOEL),
        _weigering(DOEL, "openfga_onbereikbaar"),
        {"soort": "toolfout", "tool": "notitie_toevoegen", "dossier": DOEL, "tekst": "x"},
    ],
    ids=["toegestaan", "andere-weigering", "toolfout"],
)
def test_ander_verloop_op_het_doeldossier_is_een_afwijking(poging: dict[str, Any]) -> None:
    with pytest.raises(DemoAfwijking, match="afwijkend verloop"):
        injectie_uitkomst([_weigering(DOEL), poging], DOEL)
