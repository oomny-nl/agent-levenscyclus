"""Agent-assertie: zelfondertekend JWT (ES256) waarmee een agent zich bij de poort identificeert.

Eigen profiel, geïnspireerd op RFC 7523 §2.2: `iss == sub == SCIM-id`, `aud` = canonieke URL van de
poort, `iat`, `exp` (hoogstens 60 s na `iat`) en een unieke `jti`. De sleutel komt uitsluitend uit
het SCIM-record van `sub`; headerverwijzingen (`jku`, `x5u`, `x5c`, `kid`) worden genegeerd.
"""

from __future__ import annotations

import json
import math
import secrets
import time
from dataclasses import dataclass
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey
from jwt.algorithms import ECAlgorithm

ALG = "ES256"
MAX_GELDIGHEID = 60
JTI_MIN, JTI_MAX = 16, 128


class AssertieFout(Exception):
    """Ongeldige assertie; `code` is de machineleesbare reden."""

    def __init__(self, code: str, reden: str) -> None:
        super().__init__(reden)
        self.code = code
        self.reden = reden


@dataclass(frozen=True)
class Sleutelpaar:
    """P-256-sleutelpaar van een agent."""

    prive: ec.EllipticCurvePrivateKey

    @classmethod
    def nieuw(cls) -> Sleutelpaar:
        """Genereer een vers sleutelpaar."""
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def uit_pem(cls, pem: bytes) -> Sleutelpaar:
        """Laad een privésleutel uit PEM."""
        sleutel = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(sleutel, ec.EllipticCurvePrivateKey):
            raise TypeError("sleutel is geen EC-sleutel")
        return cls(sleutel)

    def pem(self) -> bytes:
        """Privésleutel als PEM (PKCS#8, onversleuteld; alleen voor de demo-volumes)."""
        return self.prive.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    def publieke_jwk(self) -> str:
        """Publieke sleutel als JWK-JSON, zoals hij in het SCIM-record komt."""
        jwk = json.loads(ECAlgorithm.to_jwk(self.prive.public_key()))
        jwk["alg"] = ALG
        jwk["use"] = "sig"
        return json.dumps(jwk, sort_keys=True)

    def assertie(self, sub: str, aud: str, geldigheid: int = MAX_GELDIGHEID) -> str:
        """Maak een assertie voor één verzoek.

        Args:
            sub: SCIM-id van de agent (ook `iss`).
            aud: Canonieke URL van de poort.
            geldigheid: Levensduur in seconden (hoogstens 60).
        """
        nu = int(time.time())
        claims = {
            "iss": sub,
            "sub": sub,
            "aud": aud,
            "iat": nu,
            "exp": nu + min(geldigheid, MAX_GELDIGHEID),
            "jti": secrets.token_urlsafe(24),
        }
        return jwt.encode(claims, self.prive, algorithm=ALG, headers={"typ": "JWT"})


def controleer_jwk(tekst: str) -> dict[str, Any]:
    """Controleer dat een `publiekeSleutel` een bruikbare publieke P-256-JWK is.

    De sleutel wordt echt opgebouwd (RFC 7518 §6.2.1): `x` en `y` moeten base64url-strings van
    32 bytes zijn en samen een punt op de kromme vormen. `alg` en `use` zijn optioneel, maar als
    ze er staan moeten ze `ES256` en `sig` zijn.

    Returns:
        De JWK als dict.

    Raises:
        ValueError: Bij ongeldige JSON, een ander sleuteltype, een andere kromme, een
            privé-onderdeel, onbruikbare coördinaten of tegenstrijdige metadata.
    """
    try:
        jwk = json.loads(tekst)
    except ValueError as fout:
        raise ValueError(f"publiekeSleutel is geen JSON: {fout}") from fout
    if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise ValueError("publiekeSleutel moet een JWK zijn met kty EC en crv P-256")
    if "d" in jwk:
        raise ValueError("publiekeSleutel bevat een privé-onderdeel (d)")
    if not all(isinstance(jwk.get(c), str) and jwk[c] for c in ("x", "y")):
        raise ValueError("publiekeSleutel mist x of y (base64url-strings)")
    if jwk.get("alg", ALG) != ALG or jwk.get("use", "sig") != "sig":
        raise ValueError(f"publiekeSleutel is alleen bruikbaar met alg {ALG} en use sig")
    try:
        sleutel = ECAlgorithm.from_jwk(jwk)
    except (jwt.InvalidKeyError, ValueError) as fout:
        raise ValueError(f"publiekeSleutel is geen geldige P-256-sleutel: {fout}") from fout
    if not isinstance(sleutel, EllipticCurvePublicKey):
        raise ValueError("publiekeSleutel is geen publieke sleutel")
    return jwk


def sub_uit_assertie(token: str) -> str:
    """Lees `sub` zonder verificatie, om het SCIM-record op te halen.

    Raises:
        AssertieFout: Als het token niet te lezen is of `iss` en `sub` niet kloppen.
    """
    try:
        header = jwt.get_unverified_header(token)
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as fout:
        raise AssertieFout("assertie_ongeldig", f"assertie onleesbaar: {fout}") from fout
    _controleer_header(header)
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub or claims.get("iss") != sub:
        raise AssertieFout("assertie_ongeldig", "iss en sub moeten aanwezig en gelijk zijn")
    return sub


def _controleer_header(header: dict[str, Any]) -> None:
    if header.get("alg") != ALG:
        raise AssertieFout("assertie_ongeldig", f"alg moet {ALG} zijn")
    if header.get("typ") != "JWT":
        raise AssertieFout("assertie_ongeldig", "typ moet JWT zijn")


def _tijdstip(claims: dict[str, Any], naam: str) -> float:
    """Een `NumericDate` (RFC 7519 §2): een eindig JSON-getal.

    PyJWT laat bij `iat` en `exp` ook een numerieke string of een boolean door; daarmee rekenen
    zou een `TypeError` geven in plaats van een gelogde weigering.
    """
    waarde = claims[naam]
    if isinstance(waarde, bool) or not isinstance(waarde, int | float) or not math.isfinite(waarde):
        raise AssertieFout("assertie_ongeldig", f"{naam} moet een getal zijn (NumericDate)")
    return float(waarde)


def _controleer_claims(claims: dict[str, Any], aud: str, max_skew: int) -> None:
    # PyJWT accepteert ook een lijst die `aud` bevat; het profiel eist precies één URL.
    if not isinstance(claims["aud"], str) or claims["aud"] != aud:
        raise AssertieFout("assertie_ongeldig", "aud moet precies de URL van de poort zijn")
    if claims["iss"] != claims["sub"]:
        raise AssertieFout("assertie_ongeldig", "iss en sub verschillen")
    iat, exp = _tijdstip(claims, "iat"), _tijdstip(claims, "exp")
    if iat > time.time() + max_skew:
        raise AssertieFout("assertie_ongeldig", "iat ligt in de toekomst")
    duur = exp - iat
    if not 0 < duur <= MAX_GELDIGHEID:
        raise AssertieFout(
            "assertie_ongeldig", f"exp - iat moet tussen 1 en {MAX_GELDIGHEID} s zijn"
        )


def verifieer_assertie(token: str, jwk: str, aud: str, max_skew: int) -> dict[str, Any]:
    """Verifieer een assertie tegen de sleutel uit het SCIM-record.

    Args:
        token: Compact JWT uit de Authorization-header.
        jwk: `publiekeSleutel` uit het SCIM-record van `sub`.
        aud: Canonieke URL van de poort.
        max_skew: Toegestane klokafwijking in seconden.

    Returns:
        De geverifieerde claims.

    Raises:
        AssertieFout: Bij elke afwijking van het profiel.
    """
    try:
        _controleer_header(jwt.get_unverified_header(token))
    except jwt.PyJWTError as fout:
        raise AssertieFout("assertie_ongeldig", f"assertie onleesbaar: {fout}") from fout
    try:
        sleutel = ECAlgorithm.from_jwk(jwk)
        if not isinstance(sleutel, EllipticCurvePublicKey):
            raise AssertieFout("assertie_ongeldig", "publiekeSleutel is geen publieke sleutel")
        claims = jwt.decode(
            token,
            sleutel,
            algorithms=[ALG],
            audience=aud,
            leeway=max_skew,
            options={"require": ["iss", "sub", "aud", "iat", "exp", "jti"]},
        )
    except jwt.ExpiredSignatureError as fout:
        raise AssertieFout("assertie_verlopen", "assertie is verlopen") from fout
    except jwt.PyJWTError as fout:
        raise AssertieFout("assertie_ongeldig", f"assertie ongeldig: {fout}") from fout
    except ValueError as fout:
        raise AssertieFout("assertie_ongeldig", f"sleutel onbruikbaar: {fout}") from fout
    _controleer_claims(claims, aud, max_skew)
    jti = claims["jti"]
    if not isinstance(jti, str) or not JTI_MIN <= len(jti) <= JTI_MAX:
        raise AssertieFout("assertie_ongeldig", f"jti moet {JTI_MIN} tot {JTI_MAX} tekens zijn")
    return claims
