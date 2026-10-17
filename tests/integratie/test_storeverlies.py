"""Verlies van de OpenFGA-store door een herstart van de in-memory datastore (ASI08).

Twee fasen, door `make integratie` rond een `docker compose restart openfga` gedraaid:

- `STOREVERLIES_FASE=voor`: een actieve agent doet een toolcall; id, sleutel en store-id gaan naar
  het volume `/proef` (alleen in de testcontainer).
- `STOREVERLIES_FASE=na`: de eerste toolcall na de herstart moet slagen zonder handmatige actie.

Een `DELETE /stores/{id}` is geen goede nabootsing: OpenFGA verwijdert zacht en blijft checks op
het oude store-id beantwoorden (gemeten met v1.22.0), zodat niemand het verlies opmerkt.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from levenscyclus.assertie import Sleutelpaar
from tests.integratie.conftest import Keten, ProefAgent

pytestmark = [
    pytest.mark.integratie,
    pytest.mark.skipif(
        os.environ.get("STOREVERLIES_FASE") not in {"voor", "na"},
        reason="draait alleen in de storeverliesfasen van make integratie",
    ),
]

DOSSIER = "2026-0142"
TOESTAND = Path("/proef/storeverlies.json")


def test_storeverlies_voorbereiding(keten: Keten) -> None:
    if os.environ["STOREVERLIES_FASE"] != "voor":
        pytest.skip("alleen in fase voor")
    agent = keten.actieve_agent()
    assert keten.toolcall(agent, "dossier_lezen", DOSSIER).status == 200
    TOESTAND.write_text(
        json.dumps(
            {
                "id": agent.id,
                "naam": agent.naam,
                "eigenaar": agent.eigenaar,
                "pem": agent.paar.pem().decode(),
                "store_id": keten.beheer.openfga()["store_id"],
            }
        ),
        encoding="utf-8",
    )


def test_openfga_storeverlies_hersteld(keten: Keten) -> None:
    if os.environ["STOREVERLIES_FASE"] != "na":
        pytest.skip("alleen in fase na")
    toestand = json.loads(TOESTAND.read_text(encoding="utf-8"))
    paar = Sleutelpaar.uit_pem(toestand["pem"].encode())
    agent = ProefAgent(toestand["id"], toestand["naam"], toestand["eigenaar"], paar)
    # Eerste aanraking van OpenFGA na de herstart: de poort krijgt `store_id_not_found`, vraagt
    # het register om de id's (dat maakt store en model opnieuw en herbouwt zijn tuples) en
    # schrijft haar dossierrelaties opnieuw.
    antwoord = keten.toolcall(agent, "dossier_lezen", DOSSIER)
    assert antwoord.status == 200, antwoord.tekst
    assert keten.beheer.openfga()["store_id"] != toestand["store_id"]
    assert keten.beheer.relatiecheck(agent.id, DOSSIER)["allowed"] is True
    record = keten.toolrecords(agent)[-1]
    assert (record["beslissing"], record["controles"]["relatie"]) == ("toegestaan", "ok")
