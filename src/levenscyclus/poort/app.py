"""De poort: MCP-server (streamable HTTP) achter een ASGI-middleware die elke toolcall toetst.

Tools: `dossier_lezen(dossier)` en `notitie_toevoegen(dossier, tekst)`. Een besluit vastleggen is
geen tool: dat blijft een menselijke handeling. `GET /dossiers/{nr}/notities` (beheerderstoken)
dient de neveneffectcontrole in demo en tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from levenscyclus.config import Instellingen
from levenscyclus.fga import Fga, FgaFout, FgaIds
from levenscyclus.klok import iso
from levenscyclus.poort.gegevens import DOSSIERS
from levenscyclus.poort.opslag import PoortOpslag
from levenscyclus.poort.register import Register, RegisterFout
from levenscyclus.poort.toegang import Agent, Besluit, Toegang

log = logging.getLogger("poort")
MAX_BODY = 64 * 1024


class TeGroot(Exception):
    """Verzoek groter dan MAX_BODY."""


async def _lees_body(receive: Receive) -> bytes:
    delen: list[bytes] = []
    grootte = 0
    while True:
        bericht = await receive()
        deel = bericht.get("body", b"")
        grootte += len(deel)
        if grootte > MAX_BODY:
            raise TeGroot
        delen.append(deel)
        if not bericht.get("more_body"):
            return b"".join(delen)


def toolaanroep(body: bytes) -> tuple[str | None, str | None]:
    """Toolnaam en dossier uit een JSON-RPC `tools/call`, anders (None, None)."""
    try:
        verzoek = json.loads(body)
    except ValueError:
        return None, None
    if not isinstance(verzoek, dict) or verzoek.get("method") != "tools/call":
        return None, None
    params = verzoek.get("params")
    if not isinstance(params, dict):
        return "(ongeldig)", None
    argumenten = params.get("arguments")
    dossier = argumenten.get("dossier") if isinstance(argumenten, dict) else None
    return str(params.get("name")), str(dossier) if dossier else None


Starter = Callable[[], Awaitable[None]]


class Poort:
    """ASGI-middleware die `/mcp` afschermt met de vijf controles.

    Args:
        app: De MCP-app.
        toegang: De controles.
        start: Coroutine die bij `lifespan.startup` draait (id's ophalen, dossiers synchroniseren);
            zo gebruikt alle asynchrone toestand de event loop van de server.
    """

    def __init__(self, app: ASGIApp, toegang: Toegang, start: Starter | None = None) -> None:
        self.app = app
        self.toegang = toegang
        self._start = start

    async def _lifespan(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def ontvang() -> Message:
            bericht = await receive()
            if bericht["type"] == "lifespan.startup" and self._start is not None:
                await self._start()
            return bericht

        await self.app(scope, ontvang, send)

    @staticmethod
    async def _weiger(send: Send, besluit: Besluit) -> None:
        fout = besluit.fout
        if fout is None:
            raise RuntimeError("weigering zonder fout")
        reden = fout.reden.replace('"', "'")
        kop = f'Bearer error="{fout.code}", error_description="{reden}"'
        body = json.dumps(
            {"error": fout.code, "error_description": fout.reden, "stap": fout.stap}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": fout.status,
                "headers": [
                    (b"www-authenticate", kop.encode()),
                    (b"content-type", b"application/json"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Verwerk een ASGI-verzoek."""
        if scope["type"] == "lifespan":
            await self._lifespan(scope, receive, send)
            return
        if scope["type"] != "http" or not scope["path"].startswith("/mcp"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        try:
            body = await _lees_body(receive)
        except TeGroot:
            await send({"type": "http.response.start", "status": 413, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        tool, dossier = toolaanroep(body)
        besluit = await self.toegang.controleer(headers.get("authorization", ""), tool, dossier)
        if not besluit.toegestaan:
            await self._weiger(send, besluit)
            return
        scope.setdefault("state", {})["agent"] = besluit.agent
        verzonden = False

        async def herhaal() -> Message:
            nonlocal verzonden
            if verzonden:
                return await receive()
            verzonden = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, herhaal, send)


def _agent(ctx: Context) -> Agent:
    request = ctx.request_context.request
    if request is None:
        raise ToolError("geen HTTP-verzoek bij deze toolaanroep; alleen streamable HTTP")
    return request.scope["state"]["agent"]


def maak_mcp(opslag: PoortOpslag, register: Register) -> MCPServer:
    """Bouw de MCP-server met de twee tools en de beheerroutes."""
    mcp = MCPServer(
        "dossiers-lindevoort",
        instructions="Dossiers van de fictieve gemeente Lindevoort; de poort toetst elke toolcall.",
    )

    @mcp.tool(description="Lees een dossier (aanvraag, stukken, bijlagen, notities).")
    async def dossier_lezen(dossier: str, ctx: Context) -> str:
        _agent(ctx)
        inhoud = {**DOSSIERS[dossier], "notities": opslag.notities(dossier)}
        return json.dumps(inhoud, ensure_ascii=False, indent=2)

    @mcp.tool(description="Zet een conceptnotitie in een dossier.")
    async def notitie_toevoegen(dossier: str, tekst: str, ctx: Context) -> str:
        agent = _agent(ctx)
        nr = opslag.voeg_notitie_toe(
            dossier,
            tekst,
            {"id": agent.id, "naam": agent.naam, "eigenaar_naam": agent.eigenaar_naam},
            iso(datetime.now(UTC)),
        )
        return f"Notitie {nr} toegevoegd aan {dossier} (concept, door {agent.naam})"

    @mcp.custom_route("/dossiers/{dossier}/notities", methods=["GET"])
    async def notities(request: Request) -> Response:
        try:
            beheerder = await register.is_beheerder(request.headers.get("authorization", ""))
        except RegisterFout as fout:
            return JSONResponse({"error": str(fout)}, status_code=503)
        if not beheerder:
            return JSONResponse({"error": "beheerderstoken vereist"}, status_code=403)
        dossier = request.path_params["dossier"]
        if dossier not in DOSSIERS:
            return JSONResponse({"error": "dossier onbekend"}, status_code=404)
        return JSONResponse({"dossier": dossier, "notities": opslag.notities(dossier)})

    @mcp.custom_route("/health", methods=["GET"])
    async def gezondheid(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    return mcp


def maak_app(
    toegang: Toegang, opslag: PoortOpslag, register: Register, start: Starter | None = None
) -> Poort:
    """De volledige ASGI-app: MCP plus routes, achter de poort."""
    mcp = maak_mcp(opslag, register)
    app = mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")  # noqa: S104
    return Poort(app, toegang, start)


def maak_toegang(inst: Instellingen, api_token: str) -> tuple[Toegang, PoortOpslag, Register]:
    """Bouw de controles uit de omgeving; de id's komen pas bij de start van het register."""
    token = (inst.geheimen_dir / "poort.token").read_text(encoding="utf-8").strip()
    register = Register(inst.register_url, token)
    fga = Fga(inst.openfga_url, api_token, FgaIds("", ""))
    opslag = PoortOpslag(inst.db_pad)
    return Toegang(inst, register, fga, opslag), opslag, register


async def start_toegang(toegang: Toegang, pogingen: int = 30) -> None:
    """Haal store- en model-id bij het register en schrijf `dossier#team`; wacht op het register."""
    for poging in range(pogingen):
        try:
            await toegang.herstel()
            return
        except (RegisterFout, FgaFout) as fout:
            if poging == pogingen - 1:
                raise
            log.warning("register of OpenFGA nog niet gereed (%s); opnieuw over 2 s", fout)
            await asyncio.sleep(2)


def main() -> None:
    """Start de poort (in de container op poort 8000)."""
    logging.basicConfig(level=logging.INFO)
    inst = Instellingen.uit_omgeving()
    sleutel = os.environ.get("OPENFGA_PRESHARED_KEY", "")
    if not sleutel:
        raise SystemExit("OPENFGA_PRESHARED_KEY ontbreekt; `make` zet hem in .env")
    toegang, opslag, register = maak_toegang(inst, sleutel)

    async def start() -> None:
        await start_toegang(toegang)

    app = maak_app(toegang, opslag, register, start)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")  # noqa: S104


__all__ = ["Poort", "maak_app", "maak_mcp", "maak_toegang", "main", "start_toegang", "toolaanroep"]
