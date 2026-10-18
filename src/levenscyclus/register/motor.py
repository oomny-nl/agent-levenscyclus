"""Levenscyclusmotor: effectieve status, overgangen en tuplesynchronisatie (ONTWERP §4.2, §4.3).

Consistentiecontract: weg van `actief` worden SCIM-mutatie en de nieuwe status van alle betrokken
agents in één transactie opgeslagen (`blokkeer_*`/`registreer_*`, synchroon) en daarna de relaties
gesynchroniseerd (`sync_*`, asynchroon); naar `actief` wordt eerst gesynchroniseerd en
teruggelezen, dan opgeslagen. Synchronisatie leest per subject, zodat relaties op voormalige
objecten ook verdwijnen.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from levenscyclus.config import Instellingen
from levenscyclus.duur import parse_duur
from levenscyclus.fga import Fga, FgaFout, Tuple
from levenscyclus.klok import Klok, iso, parse_tijd
from levenscyclus.register.opslag import Opslag
from levenscyclus.register.scim import (
    AGENT_CONFIGURATIE,
    URN_AGENT,
    ScimFout,
    is_agent,
    leden_van,
)

log = logging.getLogger("motor")

ACTIEF = "actief"
HERBEOORDELING = "herbeoordeling_vereist"
GEDEACTIVEERD = "gedeactiveerd"
VERLOPEN = "verlopen"
NOODSTOP = "uitgeschakeld_door_beheerder"
INGESCHAKELD = (ACTIEF, HERBEOORDELING)
STATUSVELDEN = (
    "levenscyclusStatus",
    "statusReden",
    "statusSinds",
    "laatsteBevestiging",
    "bevestigdeVersie",
    "volgendeHerbeoordeling",
    "bevestigingsdeadline",
)


@dataclass(frozen=True)
class Overgang:
    """Eén statusovergang, zoals de controle die teruggeeft."""

    agent: str
    agent_naam: str
    van: str
    naar: str
    reden: str
    sinds: str


def ext(agent: dict[str, Any]) -> dict[str, Any]:
    """De agentextensie van een User-resource."""
    return agent.setdefault(URN_AGENT, {})


def is_noodstop(agent: dict[str, Any]) -> bool:
    """Of de opgeslagen status een noodstop is (dominant)."""
    e = ext(agent)
    return e.get("levenscyclusStatus") == GEDEACTIVEERD and e.get("statusReden") == NOODSTOP


def effectief(agent: dict[str, Any], nu: datetime, standaard_termijn: str) -> dict[str, Any]:
    """Bepaal de effectieve status op `nu` (tijdregels 1 tot 5 uit het ontwerp) op een kopie.

    Args:
        agent: Opgeslagen User-resource van een agent.
        nu: Registertijd.
        standaard_termijn: Standaard `bevestigingstermijn` als het record er geen heeft.

    Returns:
        Kopie van de resource met effectieve `levenscyclusStatus`, `statusReden`, `statusSinds`,
        `bevestigingsdeadline`, `beoordeeldOp` en afgeleid `active`.
    """
    kopie = copy.deepcopy(agent)
    e = ext(kopie)
    e["beoordeeldOp"] = iso(nu)
    if not is_noodstop(kopie):
        _pas_tijdregels_toe(e, nu, standaard_termijn)
    kopie["active"] = e.get("levenscyclusStatus") in INGESCHAKELD
    return kopie


def _pas_tijdregels_toe(e: dict[str, Any], nu: datetime, standaard_termijn: str) -> None:
    verval = parse_tijd(e["vervaldatum"])
    if verval <= nu:
        _zet(e, VERLOPEN, "vervaldatum_bereikt", verval)
        return
    volgende = e.get("volgendeHerbeoordeling")
    if e.get("levenscyclusStatus") == ACTIEF and volgende and parse_tijd(volgende) <= nu:
        termijn = parse_duur(e.get("bevestigingstermijn", standaard_termijn))
        _zet(e, HERBEOORDELING, "periodieke_herbeoordeling", parse_tijd(volgende))
        e["bevestigingsdeadline"] = iso(parse_tijd(volgende) + termijn)
    deadline = e.get("bevestigingsdeadline")
    if e.get("levenscyclusStatus") == HERBEOORDELING and deadline and parse_tijd(deadline) <= nu:
        _zet(e, GEDEACTIVEERD, "niet_herbeoordeeld", parse_tijd(deadline))


def _zet(e: dict[str, Any], status: str, reden: str, sinds: datetime) -> None:
    e["levenscyclusStatus"] = status
    e["statusReden"] = reden
    e["statusSinds"] = iso(sinds)
    e.pop("bevestigingsdeadline", None)


def _gebeurtenis(status: str) -> str:
    return {
        ACTIEF: "bevestigd",
        HERBEOORDELING: "herbeoordeling_vereist",
        GEDEACTIVEERD: "gedeactiveerd",
        VERLOPEN: "verlopen",
    }[status]


class Motor:
    """Overgangen en synchronisatie; één instantie per registerproces.

    Args:
        opslag: Register-opslag.
        fga: OpenFGA-toegang.
        klok: Registerklok.
        instellingen: Standaardtermijnen.
    """

    def __init__(self, opslag: Opslag, fga: Fga, klok: Klok, instellingen: Instellingen) -> None:
        self.opslag = opslag
        self.fga = fga
        self.klok = klok
        self.instellingen = instellingen

    # Lezen ------------------------------------------------------------------------------------

    def effectief(self, agent: dict[str, Any]) -> dict[str, Any]:
        """Effectieve status op de huidige registertijd, verrijkt met eigenaarsgegevens."""
        kopie = effectief(agent, self.klok.nu(), self.instellingen.standaard_bevestigingstermijn)
        e = ext(kopie)
        eigenaar = self.opslag.haal("User", e["eigenaar"]["value"])
        if eigenaar is not None:
            e["eigenaar"] = {
                "value": eigenaar["id"],
                "$ref": eigenaar["meta"]["location"],
                "display": eigenaar.get("displayName") or eigenaar["userName"],
            }
        return kopie

    def eigenaar_van(self, agent: dict[str, Any]) -> dict[str, Any] | None:
        """De eigenaar-resource van een agent, of None."""
        return self.opslag.haal("User", ext(agent)["eigenaar"]["value"])

    def controleer_eigenaar(self, eigenaar_id: str) -> dict[str, Any]:
        """Eigenaar moet bestaan, actief zijn en een mens zijn.

        Raises:
            ScimFout: `400 invalidValue` anders.
        """
        eigenaar = self.opslag.haal("User", eigenaar_id)
        if eigenaar is None:
            raise ScimFout(400, f"eigenaar {eigenaar_id} bestaat niet", "invalidValue")
        if is_agent(eigenaar):
            raise ScimFout(400, "de eigenaar van een agent moet een mens zijn", "invalidValue")
        if not eigenaar.get("active", True):
            raise ScimFout(400, f"eigenaar {eigenaar['userName']} is niet actief", "invalidValue")
        return eigenaar

    def _agents_van(self, eigenaren: set[str]) -> list[dict[str, Any]]:
        return [a for a in self.opslag.agents() if ext(a)["eigenaar"]["value"] in eigenaren]

    # Status opslaan ---------------------------------------------------------------------------

    def _bewaar_status(
        self, agent: dict[str, Any], status: str, reden: str, actor: str, **velden: Any
    ) -> dict[str, Any]:
        """Sla een nieuwe opgeslagen status op (versie omhoog) en log de overgang."""
        nu = self.klok.nu()
        e = ext(agent)
        van = e.get("levenscyclusStatus", "nieuw")
        _zet(e, status, reden, nu)
        if status == HERBEOORDELING:
            termijn = parse_duur(
                e.get("bevestigingstermijn", self.instellingen.standaard_bevestigingstermijn)
            )
            e["bevestigingsdeadline"] = iso(nu + termijn)
        e.update(velden)
        agent["active"] = status in INGESCHAKELD
        with self.opslag.transactie():
            opgeslagen = self.opslag.vervang("User", agent["id"], agent, iso(nu))
            self._log_levenscyclus(opgeslagen, _gebeurtenis(status), actor, reden, van=van)
        return opgeslagen

    def _log_levenscyclus(
        self, agent: dict[str, Any], gebeurtenis: str, actor: str, reden: str, **extra: Any
    ) -> None:
        eigenaar = self.eigenaar_van(agent)
        self.opslag.log(
            {
                "soort": "levenscyclus",
                "gebeurtenis": gebeurtenis,
                "agent": agent["id"],
                "agent_naam": agent["userName"],
                "eigenaar": ext(agent)["eigenaar"]["value"],
                "eigenaar_naam": (eigenaar.get("displayName") or eigenaar["userName"])
                if eigenaar
                else None,
                "actor": actor,
                "reden": reden,
                "status": ext(agent).get("levenscyclusStatus"),
                **extra,
            },
            self.klok.nu(),
        )

    # Synchronisatie per subject ---------------------------------------------------------------

    def _gewenste_teams(self, gebruiker_id: str) -> set[Tuple]:
        return {
            Tuple(f"gebruiker:{gebruiker_id}", "lid", f"team:{g['id']}")
            for g in self.opslag.alle("Group")
            if gebruiker_id in leden_van(g)
        }

    async def _sync_teams_van(self, gebruiker_id: str) -> None:
        """Alle `team#lid`-tuples van deze gebruiker, ook op verwijderde of verlaten groepen."""
        gewenst = self._gewenste_teams(gebruiker_id) if self._is_mens(gebruiker_id) else set()
        await self.fga.synchroniseer_subject(f"gebruiker:{gebruiker_id}", "team", gewenst)

    def _is_mens(self, gebruiker_id: str) -> bool:
        gebruiker = self.opslag.haal("User", gebruiker_id)
        return gebruiker is not None and not is_agent(gebruiker)

    async def _sync_eigenaar(self, agent: dict[str, Any]) -> None:
        eigenaar = ext(agent)["eigenaar"]["value"]
        gewenst = {Tuple(f"gebruiker:{eigenaar}", "eigenaar", f"agent:{agent['id']}")}
        await self.fga.synchroniseer(f"agent:{agent['id']}", "eigenaar", gewenst)

    async def _sync_toegang(self, agent: dict[str, Any], *, als_actief: bool = False) -> None:
        """Toegangsrelaties met deze agent als subject: alleen bij `actief`, anders leeg."""
        e = ext(agent)
        actief = als_actief or e.get("levenscyclusStatus") == ACTIEF
        subject = f"agent:{agent['id']}"
        eigenaar: set[Tuple] = set()
        tools: set[Tuple] = set()
        if actief:
            eigenaar = {
                Tuple(subject, "agent", f"gebruiker:{e['eigenaar']['value']}", e["vervaldatum"])
            }
            tools = {
                Tuple(subject, "toegestaan", f"tool:{t}") for t in e.get("toegestaneTools", [])
            }
        await self.fga.synchroniseer_subject(subject, "gebruiker", eigenaar)
        await self.fga.synchroniseer_subject(subject, "tool", tools)

    async def _sync_gelogd(self, agents: list[dict[str, Any]], eigenaren: set[str]) -> None:
        """Toegang van deze agents en teams van deze eigenaren synchroniseren; fouten gelogd."""
        try:
            for eigenaar in eigenaren:
                await self._sync_teams_van(eigenaar)
            for agent in agents:
                await self._sync_toegang(agent)
        except FgaFout as fout:
            log.error("tuples niet gesynchroniseerd: %s", fout)
            for agent in agents:
                self._log_levenscyclus(agent, "tuples_niet_gesynchroniseerd", "register", str(fout))

    async def herbouw_alles(self) -> None:
        """Alle register-tuples uit de SCIM-records afleiden (controle, herstel na storeverlies)."""
        for gebruiker in self.opslag.alle("User"):
            if not is_agent(gebruiker):
                await self._sync_teams_van(gebruiker["id"])
        for agent in self.opslag.agents():
            await self._sync_eigenaar(agent)
            await self._sync_toegang(agent)

    # Joiner -----------------------------------------------------------------------------------

    def registreer_aanmaak(self, agent: dict[str, Any], actor: str) -> dict[str, Any]:
        """Joiner, in de transactie van de `POST`: status `herbeoordeling_vereist`."""
        if parse_tijd(ext(agent)["vervaldatum"]) <= self.klok.nu():
            raise ScimFout(400, "vervaldatum moet in de toekomst liggen", "invalidValue")
        return self._bewaar_status(agent, HERBEOORDELING, "wacht_op_eigenaar", actor)

    async def sync_na_aanmaak(self, agent: dict[str, Any]) -> None:
        """Governance-relatie schrijven; een fout wordt gelogd en bij de controle hersteld."""
        try:
            await self._sync_eigenaar(agent)
        except FgaFout as fout:
            self._log_levenscyclus(agent, "tuples_niet_gesynchroniseerd", "register", str(fout))

    # Bevestiging ------------------------------------------------------------------------------

    def _controleer_bevestiging(
        self, agent: dict[str, Any], eigenaar_id: str, doel: str, versie: str
    ) -> None:
        e = ext(agent)
        if e["eigenaar"]["value"] != eigenaar_id:
            raise ScimFout(403, "alleen de eigenaar kan deze agent bevestigen")
        self.controleer_eigenaar(eigenaar_id)
        eff = ext(self.effectief(agent))
        if eff["levenscyclusStatus"] not in INGESCHAKELD:
            raise ScimFout(
                409,
                f"agent is {eff['levenscyclusStatus']} ({eff['statusReden']}); "
                "alleen de beheerder kan hem heractiveren",
            )
        if doel != e["doel"]:
            raise ScimFout(409, "doel komt niet overeen met het huidige doel van de agent")
        if versie != agent["meta"]["version"]:
            raise ScimFout(409, "versie is verouderd; lees de agent opnieuw en bevestig dan")

    async def bevestig(
        self, agent: dict[str, Any], eigenaar_id: str, doel: str, versie: str
    ) -> dict[str, Any]:
        """Bevestiging door de eigenaar; activeert pas na geslaagde synchronisatie.

        Raises:
            ScimFout: 403 (niet de eigenaar), 409 (status, verval of versie), 503 (OpenFGA).
        """
        self._controleer_bevestiging(agent, eigenaar_id, doel, versie)
        e = ext(agent)
        try:
            await self._sync_teams_van(eigenaar_id)
            await self._sync_eigenaar(agent)
            await self._sync_toegang(agent, als_actief=True)
        except FgaFout as fout:
            self._log_levenscyclus(agent, "tuples_niet_gesynchroniseerd", "eigenaar", str(fout))
            raise ScimFout(
                503, f"relaties niet gesynchroniseerd, agent niet geactiveerd: {fout}"
            ) from fout
        nu = self.klok.nu()
        termijn = parse_duur(
            e.get("herbeoordelingTermijn", self.instellingen.standaard_herbeoordeling)
        )
        return self._bewaar_status(
            agent,
            ACTIEF,
            "bevestigd_door_eigenaar",
            "eigenaar",
            laatsteBevestiging=iso(nu),
            bevestigdeVersie=versie,
            volgendeHerbeoordeling=iso(nu + termijn),
        )

    # Mover ------------------------------------------------------------------------------------

    def blokkeer_na_groepswijziging(
        self, oud: dict[str, Any] | None, nieuw: dict[str, Any] | None
    ) -> tuple[set[str], list[dict[str, Any]]]:
        """Mover, in de transactie van de groepsmutatie.

        Returns:
            De eigenaren wier lidmaatschap feitelijk veranderde en hun geblokkeerde agents.
        """
        oude_leden = leden_van(oud) if oud else set()
        nieuwe_leden = leden_van(nieuw) if nieuw else set()
        gewijzigd = oude_leden ^ nieuwe_leden
        reden = "eigenaar_van_team_gewisseld"
        getroffen = []
        for agent in map(self._actueel, self._agents_van(gewijzigd)):
            status = ext(agent).get("levenscyclusStatus")
            if status == ACTIEF:
                getroffen.append(self._bewaar_status(agent, HERBEOORDELING, reden, "bron"))
            elif status == HERBEOORDELING:
                self._verhoog_versie(agent, reden, "bron")
        return gewijzigd, getroffen

    def _verhoog_versie(self, agent: dict[str, Any], reden: str, actor: str) -> None:
        """Nieuwe recordversie zonder statuswissel: een eerder gelezen versie bevestigt niet meer.

        Voor een agent die al op bevestiging wacht; status en bevestigingsdeadline blijven staan.
        """
        e = ext(agent)
        e["statusReden"] = reden
        with self.opslag.transactie():
            opgeslagen = self.opslag.vervang("User", agent["id"], agent, iso(self.klok.nu()))
            self._log_levenscyclus(opgeslagen, HERBEOORDELING, actor, reden, van=HERBEOORDELING)

    async def sync_na_groepswijziging(
        self, eigenaren: set[str], getroffen: list[dict[str, Any]]
    ) -> None:
        """Teamtuples van de gewijzigde leden en toegang van de geblokkeerde agents."""
        await self._sync_gelogd(getroffen, eigenaren)

    # Leaver -----------------------------------------------------------------------------------

    def blokkeer_na_gebruikerswijziging(
        self, oud: dict[str, Any], nieuw: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """Leaver, in de transactie van de gebruikersmutatie: agents van de eigenaar uit."""
        was_actief = oud.get("active", True)
        is_nu_actief = nieuw is not None and nieuw.get("active", True)
        if not was_actief or is_nu_actief:
            return []
        return [
            self._bewaar_status(agent, GEDEACTIVEERD, "eigenaar_vertrokken", "bron")
            for agent in map(self._actueel, self._agents_van({oud["id"]}))
            if ext(agent).get("levenscyclusStatus") in INGESCHAKELD and not is_noodstop(agent)
        ]

    async def sync_na_gebruikerswijziging(
        self, gebruiker_id: str, getroffen: list[dict[str, Any]]
    ) -> None:
        """Teams van de gebruiker (leeg na verwijdering) en toegang van zijn agents."""
        await self._sync_gelogd(getroffen, {gebruiker_id})

    # Beheerderswijziging ----------------------------------------------------------------------

    def registreer_agentwijziging(
        self, oud: dict[str, Any], nieuw: dict[str, Any], actor: str, schakelaar: bool | None
    ) -> dict[str, Any]:
        """Beheerderswijziging, in de transactie van de `PUT`/`PATCH`.

        Eigenaarswissel, configuratiewijziging, noodstop en heractivering; opgeslagen
        statusvelden blijven behouden (readOnly). `schakelaar` is de expliciete `active`-opdracht
        van de beheerder (None als hij `active` niet zet); hij wordt nooit afgeleid uit een
        verschil tussen opgeslagen en effectieve toestand.
        """
        oud = self._actueel(oud)
        oe, ne = ext(oud), ext(nieuw)
        for sleutel in STATUSVELDEN:
            if sleutel in oe:
                ne[sleutel] = oe[sleutel]
            else:
                ne.pop(sleutel, None)
        nieuw["active"] = oud.get("active", True)
        nieuwe_eigenaar = ne["eigenaar"]["value"] != oe["eigenaar"]["value"]
        if nieuwe_eigenaar:
            self.controleer_eigenaar(ne["eigenaar"]["value"])
        configuratie = any(oe.get(k) != ne.get(k) for k in AGENT_CONFIGURATIE)
        opgeslagen = self.opslag.vervang("User", oud["id"], nieuw, iso(self.klok.nu()))
        opgeslagen = self._verwerk_schakelaar(opgeslagen, schakelaar, actor)
        if is_noodstop(opgeslagen):
            return opgeslagen
        status = ext(opgeslagen).get("levenscyclusStatus")
        if nieuwe_eigenaar:
            return self._bewaar_status(opgeslagen, HERBEOORDELING, "nieuwe_eigenaar", actor)
        if configuratie and status == ACTIEF:
            return self._bewaar_status(opgeslagen, HERBEOORDELING, "configuratie_gewijzigd", actor)
        if configuratie and status == VERLOPEN:
            return self._bewaar_status(opgeslagen, HERBEOORDELING, "vervaldatum_verlengd", actor)
        return opgeslagen

    async def sync_na_agentwijziging(self, agent: dict[str, Any]) -> None:
        """Governance-relatie (nieuwe eigenaar) en toegang van de agent."""
        try:
            await self._sync_eigenaar(agent)
        except FgaFout as fout:
            self._log_levenscyclus(agent, "tuples_niet_gesynchroniseerd", "register", str(fout))
            return
        await self._sync_gelogd([agent], set())

    def _verwerk_schakelaar(
        self, agent: dict[str, Any], schakelaar: bool | None, actor: str
    ) -> dict[str, Any]:
        """Noodstop bij expliciet `active=false`, in elke status; heractivering bij `true`."""
        if schakelaar is False and not is_noodstop(agent):
            return self._bewaar_status(agent, GEDEACTIVEERD, NOODSTOP, actor)
        ingeschakeld = ext(self.effectief(agent))["levenscyclusStatus"] in INGESCHAKELD
        if schakelaar is True and not ingeschakeld:
            try:
                self.controleer_eigenaar(ext(agent)["eigenaar"]["value"])
            except ScimFout as fout:
                raise ScimFout(409, f"heractiveren kan niet: {fout.detail}") from fout
            return self._bewaar_status(agent, HERBEOORDELING, "heractiveerd_door_beheerder", actor)
        return agent

    # Verwijderen ------------------------------------------------------------------------------

    def registreer_verwijdering(self, agent: dict[str, Any], actor: str) -> None:
        """Log de verwijdering (in de transactie van de `DELETE`)."""
        self._log_levenscyclus(agent, "verwijderd", actor, "verwijderd_door_beheerder")

    async def sync_na_verwijdering(self, agent: dict[str, Any]) -> None:
        """Alle tuples van de agent weg: toegang (per subject) en governance (per object)."""
        try:
            subject = f"agent:{agent['id']}"
            await self.fga.synchroniseer_subject(subject, "gebruiker", set())
            await self.fga.synchroniseer_subject(subject, "tool", set())
            await self.fga.verwijder_alles(subject)
        except FgaFout as fout:
            log.error("tuples van verwijderde agent niet opgeruimd: %s", fout)

    # Controle en toelating --------------------------------------------------------------------

    def _actueel(self, agent: dict[str, Any]) -> dict[str, Any]:
        """De agent met een al ingetreden tijdregel opgeslagen, vóór een gebeurtenis erop inwerkt.

        Zo bouwt een groepswijziging, leaver of beheerderswijziging voort op de effectieve status:
        een agent die al is verlopen of gedeactiveerd, komt niet terug in `herbeoordeling_vereist`,
        en de uitkomst hangt niet af van of de controle al heeft gedraaid.
        """
        return self._maak_persistent(agent, "register")[0]

    def _maak_persistent(
        self, agent: dict[str, Any], actor: str = "controle"
    ) -> tuple[dict[str, Any], Overgang | None]:
        eff = ext(self.effectief(agent))
        e = ext(agent)
        if (eff["levenscyclusStatus"], eff["statusReden"]) == (
            e.get("levenscyclusStatus"),
            e.get("statusReden"),
        ):
            return agent, None
        van = str(e.get("levenscyclusStatus"))
        for sleutel in ("levenscyclusStatus", "statusReden", "statusSinds"):
            e[sleutel] = eff[sleutel]
        if "bevestigingsdeadline" in eff:
            e["bevestigingsdeadline"] = eff["bevestigingsdeadline"]
        else:
            e.pop("bevestigingsdeadline", None)
        agent["active"] = eff["levenscyclusStatus"] in INGESCHAKELD
        with self.opslag.transactie():
            opgeslagen = self.opslag.vervang("User", agent["id"], agent, iso(self.klok.nu()))
            self._log_levenscyclus(
                opgeslagen,
                _gebeurtenis(eff["levenscyclusStatus"]),
                actor,
                eff["statusReden"],
                van=van,
            )
        return opgeslagen, Overgang(
            agent["id"],
            agent["userName"],
            van,
            eff["levenscyclusStatus"],
            eff["statusReden"],
            eff["statusSinds"],
        )

    def maak_persistent(self) -> list[Overgang]:
        """Maak de effectieve status van elke agent persistent en log elke overgang."""
        overgangen = [self._maak_persistent(agent)[1] for agent in self.opslag.agents()]
        return [o for o in overgangen if o is not None]

    async def controle(self) -> list[Overgang]:
        """Controle: statussen persistent maken, daarna alle tuples herbouwen."""
        overgangen = self.maak_persistent()
        await self.herbouw_alles()
        return overgangen

    def controleer_toelating(self, record: dict[str, Any]) -> str | None:
        """Definitieve toelating onder het mutatieslot (ONTWERP §4.4 stap 5).

        Returns:
            None als de toelating standhoudt, anders de reden (`toestand_gewijzigd`).
        """
        agent = self.opslag.haal("User", str(record.get("agent", "")))
        if agent is None or not is_agent(agent):
            return "toestand_gewijzigd: agent bestaat niet meer"
        if agent["meta"]["version"] != record.get("recordversie"):
            return "toestand_gewijzigd: recordversie verouderd"
        if ext(self.effectief(agent))["levenscyclusStatus"] != ACTIEF:
            return "toestand_gewijzigd: agent is niet meer actief"
        return None

    async def relatiecheck(self, agent: dict[str, Any], relatie: str, dossier: str) -> bool:
        """Vraag OpenFGA rechtstreeks, met de registertijd als `huidige_tijd`."""
        return await self.fga.check(
            f"agent:{agent['id']}",
            relatie,
            f"dossier:{dossier}",
            {"huidige_tijd": iso(self.klok.nu())},
        )
