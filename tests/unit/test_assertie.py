"""Profiel van de agent-assertie (NHI4): ES256, aud, looptijd, iat, jti, iss gelijk aan sub."""

from __future__ import annotations

import base64
import json
import secrets
import time
from typing import Any

import jwt
import pytest

from levenscyclus.assertie import (
    ALG,
    AssertieFout,
    Sleutelpaar,
    sub_uit_assertie,
    verifieer_assertie,
)

AUD = "http://poort.test/mcp"
SUB = "agent-1"
SKEW = 5


@pytest.fixture(scope="module")
def paar() -> Sleutelpaar:
    return Sleutelpaar.nieuw()


def _token(paar: Sleutelpaar, **claims: Any) -> str:
    nu = int(time.time())
    basis = {
        "iss": SUB,
        "sub": SUB,
        "aud": AUD,
        "iat": nu,
        "exp": nu + 60,
        "jti": secrets.token_urlsafe(24),
    }
    return jwt.encode({**basis, **claims}, paar.prive, algorithm=ALG)


def _code(token: str, paar: Sleutelpaar) -> str:
    with pytest.raises(AssertieFout) as fout:
        verifieer_assertie(token, paar.publieke_jwk(), AUD, SKEW)
    return fout.value.code


def _b64(deel: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(deel).encode()).rstrip(b"=").decode()


def test_geldige_assertie(paar: Sleutelpaar) -> None:
    token = paar.assertie(SUB, AUD)
    claims = verifieer_assertie(token, paar.publieke_jwk(), AUD, SKEW)
    assert (claims["sub"], claims["aud"]) == (SUB, AUD)
    assert sub_uit_assertie(token) == SUB


def test_verlopen(paar: Sleutelpaar) -> None:
    nu = int(time.time())
    assert _code(_token(paar, iat=nu - 120, exp=nu - 60), paar) == "assertie_verlopen"


def test_exp_ver_in_de_toekomst(paar: Sleutelpaar) -> None:
    nu = int(time.time())
    assert _code(_token(paar, exp=nu + 3600), paar) == "assertie_ongeldig"
    # Ook de eigen client kapt de looptijd af op 60 s.
    claims = jwt.decode(paar.assertie(SUB, AUD, 3600), options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] == 60


def test_verkeerde_aud(paar: Sleutelpaar) -> None:
    assert _code(_token(paar, aud="http://register.test"), paar) == "assertie_ongeldig"


def test_iat_in_de_toekomst(paar: Sleutelpaar) -> None:
    nu = int(time.time())
    assert _code(_token(paar, iat=nu + 120, exp=nu + 150), paar) == "assertie_ongeldig"


@pytest.mark.parametrize("lengte", [8, 200])
def test_jti_lengte(paar: Sleutelpaar, lengte: int) -> None:
    assert _code(_token(paar, jti="j" * lengte), paar) == "assertie_ongeldig"


def test_iss_verschilt_van_sub(paar: Sleutelpaar) -> None:
    token = _token(paar, iss="iemand-anders")
    assert _code(token, paar) == "assertie_ongeldig"
    with pytest.raises(AssertieFout):
        sub_uit_assertie(token)


def test_hs256_geweigerd(paar: Sleutelpaar) -> None:
    # Klassieke algoritmeverwarring: de publieke sleutel als HMAC-geheim.
    nu = int(time.time())
    claims = {"iss": SUB, "sub": SUB, "aud": AUD, "iat": nu, "exp": nu + 60, "jti": "x" * 24}
    kop = _b64({"alg": "HS256", "typ": "JWT"})
    inhoud = _b64(claims)
    token = f"{kop}.{inhoud}.{_b64({'handtekening': 'vals'})}"
    with pytest.raises(AssertieFout) as fout:
        sub_uit_assertie(token)
    assert fout.value.code == "assertie_ongeldig"
    assert _code(token, paar) == "assertie_ongeldig"


def test_alg_none_geweigerd(paar: Sleutelpaar) -> None:
    nu = int(time.time())
    claims = {"iss": SUB, "sub": SUB, "aud": AUD, "iat": nu, "exp": nu + 60, "jti": "x" * 24}
    token = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(claims)}."
    with pytest.raises(AssertieFout):
        sub_uit_assertie(token)
    assert _code(token, paar) == "assertie_ongeldig"


def test_andere_sleutel(paar: Sleutelpaar) -> None:
    assert _code(_token(Sleutelpaar.nieuw()), paar) == "assertie_ongeldig"
