"""`levenscyclus-beheer`: de CLI van de functioneel beheerder (ONTWERP §4.7).

Subcommando's: `overzicht`, `controle`, `bevestig <agent> --als <eigenaar>`,
`logboek [--controleer]`, `klok --vooruit <duur>`. Tokens komen uit de geheimenmap.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from rich.console import Console
from rich.table import Table

from levenscyclus.beheer import Beheer, BeheerFout, Scim, controleer_logboek, lees_token
from levenscyclus.config import Instellingen

console = Console(highlight=False)


def _beheer(inst: Instellingen) -> Beheer:
    return Beheer(inst.register_url, lees_token(inst.geheimen_dir / "beheerder.token"))


def toon_overzicht(overzicht: dict[str, Any]) -> None:
    """Toon het agentoverzicht als tabel."""
    tabel = Table(title=f"Agents (registertijd {overzicht['tijd']})", header_style="bold")
    for kolom in ("agent", "eigenaar", "teams", "status", "reden", "vervaldatum", "deadline"):
        tabel.add_column(kolom)
    for a in overzicht["agents"]:
        tabel.add_row(
            a["agent"],
            str(a["eigenaar"]),
            ", ".join(a["teams"]),
            a["status"],
            str(a["reden"] or ""),
            a["vervaldatum"][:10],
            (a.get("bevestigingsdeadline") or "")[:10],
            style="red" if a["status"] not in ("actief",) else None,
        )
    console.print(tabel)


def _bevestig(inst: Instellingen, beheer: Beheer, agentnaam: str, eigenaar: str) -> None:
    regel = beheer.agent(agentnaam)
    bron = Scim(inst.scim_url, lees_token(inst.geheimen_dir / "beheerder.token"))
    mensen = bron.zoek("Users", f'userName eq "{eigenaar}"')
    if not mensen:
        raise BeheerFout(404, f"eigenaar {eigenaar!r} bestaat niet")
    token = lees_token(inst.geheimen_dir / "eigenaren" / f"{mensen[0]['id']}.token")
    uitkomst = beheer.bevestig(regel["id"], token, regel["doel"], regel["versie"])
    console.print(json.dumps(uitkomst, ensure_ascii=False, indent=2))


def _logboek(inst: Instellingen, beheer: Beheer, controleer: bool) -> None:
    records = beheer.logboek()
    for r in records:
        console.print(json.dumps(r, ensure_ascii=False))
    if controleer:
        uitkomst = controleer_logboek(records, inst.anker_pad)
        console.print(f"[bold]{len(records)} records; keten en anker: {uitkomst.reden}[/bold]")
        if not uitkomst.intact or uitkomst.niet_verankerd:
            raise SystemExit(1)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="levenscyclus-beheer", description=__doc__)
    sub = parser.add_subparsers(dest="commando", required=True)
    sub.add_parser("overzicht", help="alle agents met effectieve status")
    sub.add_parser("controle", help="statussen persistent maken, tuples herbouwen")
    bevestig = sub.add_parser("bevestig", help="bevestig een agent als eigenaar")
    bevestig.add_argument("agent", help="userName van de agent")
    bevestig.add_argument("--als", required=True, metavar="EIGENAAR", help="userName eigenaar")
    logboek = sub.add_parser("logboek", help="alle records; --controleer toetst keten en anker")
    logboek.add_argument("--controleer", action="store_true")
    klok = sub.add_parser("klok", help="demoklok vooruit (alleen met DEMO_KLOK=1)")
    klok.add_argument("--vooruit", required=True, metavar="DUUR", help="ISO 8601, bv. P31D")
    return parser


def main(argv: list[str] | None = None) -> None:
    """Startpunt van `levenscyclus-beheer`."""
    args = _parser().parse_args(argv)
    inst = Instellingen.uit_omgeving()
    try:
        beheer = _beheer(inst)
        if args.commando == "overzicht":
            toon_overzicht(beheer.overzicht())
        elif args.commando == "controle":
            console.print(json.dumps(beheer.controle(), ensure_ascii=False, indent=2))
        elif args.commando == "bevestig":
            _bevestig(inst, beheer, args.agent, args.als)
        elif args.commando == "logboek":
            _logboek(inst, beheer, args.controleer)
        elif args.commando == "klok":
            console.print(json.dumps(beheer.klok(args.vooruit), ensure_ascii=False))
    except BeheerFout as fout:
        console.print(f"[bold red]{fout}[/bold red]")
        raise SystemExit(1) from fout


if __name__ == "__main__":
    main()
