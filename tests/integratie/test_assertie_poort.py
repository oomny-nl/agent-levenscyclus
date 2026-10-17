"""Assertie en poortgrenzen tegen de draaiende poort (NHI2, NHI4, NHI9, misvormde verzoeken)."""

from __future__ import annotations

import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import jwt
import pytest

from levenscyclus.assertie import ALG, Sleutelpaar
from tests.integratie.conftest import INST, Keten, rauw

pytestmark = pytest.mark.integratie

DOSSIER = "2026-0142"
TOOLCALL = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "dossier_lezen", "arguments": {"dossier": DOSSIER}},
}


def _jwt(paar: Sleutelpaar, sub: str, **claims: Any) -> str:
    """Handgemaakte assertie: standaardclaims, te overschrijven per test."""
    nu = int(time.time())
    basis = {
        "iss": sub,
        "sub": sub,
        "aud": INST.poort_url,
        "iat": nu,
        "exp": nu + 60,
        "jti": secrets.token_urlsafe(24),
    }
    return jwt.encode({**basis, **claims}, paar.prive, algorithm=ALG, headers={"typ": "JWT"})


def test_hergebruikte_assertie(keten: Keten) -> None:
    agent = keten.actieve_agent()
    assertie = agent.paar.assertie(agent.id, INST.poort_url)
    assert keten.toolcall(agent, "dossier_lezen", DOSSIER, assertie=assertie).status == 200
    tweede = keten.toolcall(agent, "dossier_lezen", DOSSIER, assertie=assertie)
    assert (tweede.status, tweede.code) == (401, "assertie_hergebruikt")
    [toegestaan] = keten.toolrecords(agent)
    assert toegestaan["beslissing"] == "toegestaan"
    # De weigering staat in het logboek, maar niet op naam: een niet geverifieerde assertie
    # wordt aan niemand toegeschreven.
    laatste = [r for r in keten.beheer.logboek() if r["soort"] == "toolcall"][-1]
    assert (laatste["agent"], laatste["reden"]) == (None, "assertie_hergebruikt")


def test_gelijktijdig_dezelfde_assertie(keten: Keten) -> None:
    agent = keten.actieve_agent()
    kop = f"Bearer {agent.paar.assertie(agent.id, INST.poort_url)}"
    with ThreadPoolExecutor(max_workers=8) as pool:
        antwoorden = list(pool.map(lambda _: rauw(TOOLCALL, kop), range(8)))
    statussen = sorted((a.status, a.code) for a in antwoorden)
    assert statussen == [(200, None)] + [(401, "assertie_hergebruikt")] * 7


def test_verlopen_assertie(keten: Keten) -> None:
    agent = keten.actieve_agent()
    nu = int(time.time())
    verlopen = _jwt(agent.paar, agent.id, iat=nu - 120, exp=nu - 60)
    antwoord = keten.toolcall(agent, "dossier_lezen", DOSSIER, assertie=verlopen)
    assert (antwoord.status, antwoord.code) == (401, "assertie_verlopen")
    te_lang = _jwt(agent.paar, agent.id, exp=nu + 3600)
    antwoord = keten.toolcall(agent, "dossier_lezen", DOSSIER, assertie=te_lang)
    assert (antwoord.status, antwoord.code) == (401, "assertie_ongeldig")


def test_assertie_met_andere_sleutel(keten: Keten) -> None:
    agent_a = keten.actieve_agent()
    agent_b = keten.actieve_agent()
    # Agent B tekent een assertie die zegt dat hij agent A is.
    vals = _jwt(agent_b.paar, agent_a.id)
    antwoord = keten.toolcall(agent_a, "dossier_lezen", DOSSIER, assertie=vals)
    assert (antwoord.status, antwoord.code) == (401, "assertie_ongeldig")
    vreemd = _jwt(Sleutelpaar.nieuw(), agent_a.id)
    antwoord = keten.toolcall(agent_a, "dossier_lezen", DOSSIER, assertie=vreemd)
    assert (antwoord.status, antwoord.code) == (401, "assertie_ongeldig")


def test_assertie_van_onbekende_of_mens(keten: Keten) -> None:
    paar = Sleutelpaar.nieuw()
    onbekend = rauw(TOOLCALL, f"Bearer {_jwt(paar, 'bestaat-niet-' + secrets.token_hex(4))}")
    assert (onbekend.status, onbekend.code) == (401, "agent_onbekend")
    mens = keten.mens()
    als_mens = rauw(TOOLCALL, f"Bearer {_jwt(paar, mens)}")
    assert (als_mens.status, als_mens.code) == (401, "agent_onbekend")


def test_misvormd_verzoek(keten: Keten) -> None:
    zonder = rauw(TOOLCALL, None)
    assert (zonder.status, zonder.code) == (401, "assertie_ontbreekt")
    rommel = rauw(b"\x00{niet-json", None)
    assert (rommel.status, rommel.code) == (401, "assertie_ontbreekt")
    onleesbaar = rauw(b"{]", "Bearer geen.jwt.token")
    assert (onleesbaar.status, onleesbaar.code) == (401, "assertie_ongeldig")
    groot = json.dumps({**TOOLCALL, "opvulling": "x" * (65 * 1024)}).encode()
    te_groot = rauw(groot, None)
    assert te_groot.status == 413
    # De poort leeft nog en verwerkt een normaal verzoek.
    agent = keten.actieve_agent()
    assert keten.toolcall(agent, "dossier_lezen", DOSSIER).status == 200
