"""Hashketen en anker van het logboek tegen de echte keten (ASI10)."""

from __future__ import annotations

import pytest

from levenscyclus.beheer import controleer_logboek
from tests.integratie.conftest import INST, Keten

pytestmark = pytest.mark.integratie

DOSSIER = "2026-0142"


def test_keten_en_anker_intact(keten: Keten) -> None:
    agent = keten.actieve_agent()
    assert keten.toolcall(agent, "dossier_lezen", DOSSIER).status == 200
    records = keten.beheer.logboek()
    controle = controleer_logboek(records, INST.anker_pad)
    assert (controle.intact, controle.reden, controle.niet_verankerd) == (True, "intact", 0)
    soorten = {r["soort"] for r in records}
    assert {"scim", "levenscyclus", "toolcall"} <= soorten
