"""MCP-verbinding van de agent met per verzoek een verse, kortlevende assertie (ONTWERP §4.4).

De agent kent zijn SCIM-id en zijn privésleutel en niets anders: geen token voor het register,
geen route naar OpenFGA. Elk HTTP-verzoek naar de poort draagt één eigen ES256-assertie met een
unieke `jti`; de poort accepteert elke `jti` één keer.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx2
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from levenscyclus.assertie import Sleutelpaar

_CODE = re.compile(r'error="([^"]*)"')
_REDEN = re.compile(r'error_description="([^"]*)"')


@dataclass(frozen=True)
class Weigering:
    """Een HTTP-weigering van de poort, uit de `WWW-Authenticate`-kop.

    Attributes:
        status: HTTP-status (401 of 403, 503 bij storing).
        code: Machineleesbare reden (`relatie_ontbreekt`, `agent_gedeactiveerd`, ...).
        reden: Toelichting.
    """

    status: int
    code: str
    reden: str

    @classmethod
    def uit_antwoord(cls, antwoord: httpx2.Response) -> Weigering:
        """Lees code en toelichting uit de `WWW-Authenticate`-challenge (de body is een stream)."""
        challenge = antwoord.headers.get("www-authenticate", "")
        code = _CODE.search(challenge)
        reden = _REDEN.search(challenge)
        if code is None:
            return cls(antwoord.status_code, "onbekend", "HTTP-weigering zonder toelichting")
        return cls(antwoord.status_code, code.group(1), reden.group(1) if reden else "")


@dataclass
class AgentAuth(httpx2.Auth):
    """httpx2-authenticatie: zet op elk verzoek een verse assertie als Bearer.

    Attributes:
        sleutelpaar: Privésleutel van de agent.
        sub: SCIM-id van de agent.
        aud: Canonieke URL van de poort.
        geldigheid: Levensduur van de assertie in seconden.
    """

    sleutelpaar: Sleutelpaar
    sub: str
    aud: str
    geldigheid: int = 60
    requires_request_body = True
    laatste_weigering: Weigering | None = field(default=None, init=False)
    laatste_methode: str | None = field(default=None, init=False)

    def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response]:
        """Voeg de assertie toe en onthoud een eventuele weigering."""
        self.laatste_methode = None
        if request.content:
            try:
                self.laatste_methode = json.loads(request.content).get("method")
            except (ValueError, AttributeError):
                self.laatste_methode = None
        request.headers["Authorization"] = (
            f"Bearer {self.sleutelpaar.assertie(self.sub, self.aud, self.geldigheid)}"
        )
        antwoord = yield request
        if antwoord.status_code >= 400:
            self.laatste_weigering = Weigering.uit_antwoord(antwoord)


def weigering_in(fout: BaseException) -> bool:
    """Of een (eventueel geneste) uitzondering een MCP-fout is, zoals een geweigerde initialize."""
    if isinstance(fout, MCPError):
        return True
    if isinstance(fout, BaseExceptionGroup):
        return fout.subgroup(MCPError) is not None
    return False


@asynccontextmanager
async def verbind(poort_url: str, auth: AgentAuth) -> AsyncGenerator[Client]:
    """Open een MCP-sessie met de poort als deze agent.

    De `initialize` ondergaat al de controles op assertie en levenscyclus; een geweigerde agent
    komt niet eens tot een sessie. De weigering staat dan in `auth.laatste_weigering`.

    Args:
        poort_url: Canonieke URL van de poort (verbinding én `aud`).
        auth: Authenticatie van de agent.
    """
    async with (
        httpx2.AsyncClient(auth=auth, timeout=30) as http,
        Client(streamable_http_client(poort_url, http_client=http)) as client,
    ):
        yield client
