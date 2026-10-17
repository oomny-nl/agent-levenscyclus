"""De vijf controles per toolcall (ONTWERP §4.4): assertie, levenscyclus, tool, relatie, logboek.

De eerste weigering bepaalt de reden; overgeslagen stappen staan als `niet_uitgevoerd` in het
record. Register en OpenFGA onbereikbaar: fail closed. Verlies van de OpenFGA-store: één herstel
via het register, dan één herhaling.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from levenscyclus.assertie import AssertieFout, sub_uit_assertie, verifieer_assertie
from levenscyclus.config import Instellingen
from levenscyclus.fga import Fga, FgaFout, FgaIds, Tuple
from levenscyclus.poort.gegevens import DOSSIERS, TOOLS
from levenscyclus.poort.opslag import PoortOpslag
from levenscyclus.poort.register import Register, RegisterFout
from levenscyclus.register.scim import URN_AGENT

log = logging.getLogger("poort")
STAPPEN = ("assertie", "levenscyclus", "tool", "relatie", "logboek")
STATUSREDEN = {
    "gedeactiveerd": "agent_gedeactiveerd",
    "herbeoordeling_vereist": "herbeoordeling_vereist",
    "verlopen": "agent_verlopen",
}


class Geweigerd(Exception):
    """Eén controle wees de aanroep af.

    Attributes:
        stap: Naam van de controle.
        code: Machineleesbare reden.
        reden: Toelichting voor mens en logboek.
        status: HTTP-status (401 identiteit, 403 autorisatie, 503 afhankelijkheid).
    """

    def __init__(self, stap: str, code: str, reden: str, status: int) -> None:
        super().__init__(reden)
        self.stap, self.code, self.reden, self.status = stap, code, reden, status


@dataclass
class Agent:
    """Wat de tools over de toegelaten agent mogen weten."""

    id: str
    naam: str
    eigenaar_naam: str | None
    recordversie: str
    jti: str
    beoordeeld_op: str
    record: dict[str, Any] = field(repr=False)


@dataclass
class Besluit:
    """Uitkomst van de controles, zoals naar het logboek geschreven."""

    toegestaan: bool
    agent: Agent | None
    record: dict[str, Any]
    fout: Geweigerd | None = None


class Toegang:
    """De controles, met hun afhankelijkheden.

    Args:
        inst: Instellingen (`poort_url` is de `aud`).
        register: Registerclient met het poort-token.
        fga: OpenFGA-toegang (id's komen van het register).
        opslag: Poortopslag voor `jti`-reserveringen.
    """

    def __init__(
        self, inst: Instellingen, register: Register, fga: Fga, opslag: PoortOpslag
    ) -> None:
        self.inst = inst
        self.register = register
        self.fga = fga
        self.opslag = opslag
        self._dossierteams: dict[str, str] = {}

    # Stap 1 en 2 ------------------------------------------------------------------------------

    async def _assertie(self, authorization: str, controles: dict[str, str]) -> Agent:
        if not authorization.lower().startswith("bearer "):
            raise Geweigerd("assertie", "assertie_ontbreekt", "Bearer-assertie ontbreekt", 401)
        token = authorization[7:].strip()
        try:
            sub = sub_uit_assertie(token)
        except AssertieFout as fout:
            raise Geweigerd("assertie", fout.code, fout.reden, 401) from fout
        try:
            record = await self.register.haal_agent(sub)
        except RegisterFout as fout:
            raise Geweigerd("assertie", "register_onbereikbaar", str(fout), 503) from fout
        if record is None:
            raise Geweigerd("assertie", "agent_onbekend", f"geen agent met id {sub}", 401)
        e = record[URN_AGENT]
        try:
            claims = verifieer_assertie(
                token, e["publiekeSleutel"], self.inst.poort_url, self.inst.max_skew
            )
        except AssertieFout as fout:
            raise Geweigerd("assertie", fout.code, fout.reden, 401) from fout
        if not self.opslag.reserveer_jti(
            sub, claims["jti"], int(claims["exp"]) + self.inst.max_skew
        ):
            raise Geweigerd("assertie", "assertie_hergebruikt", "deze assertie is al gebruikt", 401)
        controles["assertie"] = "ok"
        return Agent(
            id=record["id"],
            naam=record["userName"],
            eigenaar_naam=e["eigenaar"].get("display"),
            recordversie=record["meta"]["version"],
            jti=claims["jti"],
            beoordeeld_op=e["beoordeeldOp"],
            record=record,
        )

    @staticmethod
    def _levenscyclus(agent: Agent, controles: dict[str, str]) -> None:
        e = agent.record[URN_AGENT]
        status = e.get("levenscyclusStatus")
        if status == "actief" and agent.record.get("active") is True:
            controles["levenscyclus"] = "ok"
            return
        code = STATUSREDEN.get(str(status), "agent_gedeactiveerd")
        raise Geweigerd("levenscyclus", code, f"agent is {status} ({e.get('statusReden')})", 403)

    # Stap 3 en 4 ------------------------------------------------------------------------------

    async def _check(
        self, user: str, relatie: str, obj: str, context: dict[str, Any] | None
    ) -> bool:
        """OpenFGA-check met één herstelpoging bij storeverlies."""
        try:
            return await self.fga.check(user, relatie, obj, context)
        except FgaFout as fout:
            log.warning("OpenFGA-check mislukte (%s); herstel via het register", fout)
            await self.herstel()
            return await self.fga.check(user, relatie, obj, context)

    async def _tool(self, agent: Agent, tool: str, controles: dict[str, str]) -> None:
        try:
            toegestaan = await self._check(f"agent:{agent.id}", "toegestaan", f"tool:{tool}", None)
        except (FgaFout, RegisterFout) as fout:
            raise Geweigerd("tool", "openfga_onbereikbaar", str(fout), 503) from fout
        if not toegestaan:
            raise Geweigerd(
                "tool", "tool_niet_toegestaan", f"tool {tool} staat niet in toegestaneTools", 403
            )
        controles["tool"] = "ok"

    async def _relatie(
        self, agent: Agent, tool: str, dossier: str | None, controles: dict[str, str]
    ) -> None:
        relatie = TOOLS.get(tool)
        if relatie is None:
            raise Geweigerd("relatie", "tool_onbekend", f"tool {tool} bestaat niet", 403)
        if not dossier or dossier not in DOSSIERS:
            raise Geweigerd("relatie", "dossier_onbekend", f"dossier {dossier!r} bestaat niet", 403)
        try:
            if dossier not in self._dossierteams:
                await self.sync_dossiers()
            toegestaan = await self._check(
                f"agent:{agent.id}",
                relatie,
                f"dossier:{dossier}",
                {"huidige_tijd": agent.beoordeeld_op},
            )
        except (FgaFout, RegisterFout) as fout:
            raise Geweigerd("relatie", "openfga_onbereikbaar", str(fout), 503) from fout
        if not toegestaan:
            raise Geweigerd(
                "relatie",
                "relatie_ontbreekt",
                f"geen relatie {relatie} van {agent.naam} op dossier {dossier}",
                403,
            )
        controles["relatie"] = "ok"

    # Dossierrelaties en herstel ---------------------------------------------------------------

    async def sync_dossiers(self) -> None:
        """`dossier#team` voor elk dossier waarvan het team als SCIM-groep bestaat (idempotent)."""
        groepen = await self.register.groepen()
        for nr, dossier in DOSSIERS.items():
            team_id = groepen.get(dossier["team"].lower())
            if team_id is None:
                continue
            await self.fga.synchroniseer(
                f"dossier:{nr}", "team", {Tuple(f"team:{team_id}", "team", f"dossier:{nr}")}
            )
            self._dossierteams[nr] = team_id

    async def herstel(self) -> None:
        """Haal nieuwe id's bij het register (dat herstelt store en model); herschrijf dossiers."""
        store_id, model_id = await self.register.openfga_ids()
        if self.fga.vernieuw(FgaIds(store_id, model_id)):
            log.warning("OpenFGA-store of model is vernieuwd; dossierrelaties worden herschreven")
        self._dossierteams.clear()
        await self.sync_dossiers()

    # Alles samen ------------------------------------------------------------------------------

    async def controleer(
        self, authorization: str, tool: str | None, dossier: str | None
    ) -> Besluit:
        """Voer de controles uit; bij `tools/call` ook het logboekrecord (stap 5).

        Args:
            authorization: Waarde van de Authorization-header.
            tool: Toolnaam bij `tools/call`, anders None (`initialize`, `tools/list`).
            dossier: Dossiernummer uit de argumenten.

        Returns:
            Het besluit; bij een weigering met de `Geweigerd`-fout erin.
        """
        controles = dict.fromkeys(STAPPEN, "niet_uitgevoerd")
        agent: Agent | None = None
        fout: Geweigerd | None = None
        try:
            agent = await self._assertie(authorization, controles)
            self._levenscyclus(agent, controles)
            if tool is not None:
                await self._tool(agent, tool, controles)
                await self._relatie(agent, tool, dossier, controles)
        except Geweigerd as geweigerd:
            fout = geweigerd
            controles[geweigerd.stap] = "geweigerd"
        record = _record(agent, tool, dossier, controles, fout)
        if tool is None:
            return Besluit(fout is None, agent, record, fout)
        return await self._log(agent, record, fout)

    async def _log(
        self, agent: Agent | None, record: dict[str, Any], fout: Geweigerd | None
    ) -> Besluit:
        try:
            status, gelogd = await self.register.log(record)
        except RegisterFout as nietgelogd:
            weigering = Geweigerd("logboek", "audit_niet_vastgelegd", str(nietgelogd), 503)
            record["controles"]["logboek"] = "geweigerd"
            record.update(beslissing="geweigerd", reden=weigering.code)
            return Besluit(False, agent, record, weigering)
        if status == 409:
            weigering = Geweigerd("logboek", "toestand_gewijzigd", str(gelogd.get("reden")), 403)
            return Besluit(False, agent, gelogd, weigering)
        return Besluit(fout is None, agent, gelogd, fout)


def _record(
    agent: Agent | None,
    tool: str | None,
    dossier: str | None,
    controles: dict[str, str],
    fout: Geweigerd | None,
) -> dict[str, Any]:
    if fout is None and tool is not None:
        controles["logboek"] = "ok"
    return {
        "soort": "toolcall",
        "agent": agent.id if agent else None,
        "agent_naam": agent.naam if agent else None,
        "eigenaar_naam": agent.eigenaar_naam if agent else None,
        "tool": tool,
        "dossier": dossier,
        "beslissing": "geweigerd" if fout else "toegestaan",
        "reden": fout.code if fout else None,
        "toelichting": fout.reden if fout else None,
        "controles": controles,
        "recordversie": agent.recordversie if agent else None,
        "jti": agent.jti if agent else None,
    }
