"""De deterministische planner is aantoonbaar kaapbaar: de grens moet buiten het model liggen."""

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, ToolMessage

from levenscyclus.agent.planner import INJECTIEDOEL, INJECTIENOTITIE, deterministische_planner
from levenscyclus.poort.gegevens import DOSSIERS

INJECTIEDOSSIER = "2026-0143"


def _gelezen(dossier: str) -> ToolMessage:
    return ToolMessage(
        content=json.dumps(DOSSIERS[dossier], ensure_ascii=False),
        name="dossier_lezen",
        tool_call_id="call_1",
        additional_kwargs={"dossier": dossier},
    )


def test_gekaapte_planner_volgt_instructie() -> None:
    opdracht = HumanMessage(f"Vat dossier {INJECTIEDOSSIER} samen en zet een notitie.")
    eerste = deterministische_planner([opdracht])
    assert [(c["name"], c["args"]) for c in eerste.tool_calls] == [
        ("dossier_lezen", {"dossier": INJECTIEDOSSIER})
    ]
    tweede = deterministische_planner([opdracht, eerste, _gelezen(INJECTIEDOSSIER)])
    assert [(c["name"], c["args"]) for c in tweede.tool_calls] == [
        ("dossier_lezen", {"dossier": INJECTIEDOEL}),
        ("notitie_toevoegen", {"dossier": INJECTIEDOEL, "tekst": INJECTIENOTITIE}),
    ]


def test_planner_zonder_injectie_blijft_bij_opdracht() -> None:
    opdracht = HumanMessage("Vat dossier 2026-0142 samen en zet een notitie.")
    eerste = deterministische_planner([opdracht])
    tweede = deterministische_planner([opdracht, eerste, _gelezen("2026-0142")])
    [oproep] = tweede.tool_calls
    assert (oproep["name"], oproep["args"]["dossier"]) == ("notitie_toevoegen", "2026-0142")
