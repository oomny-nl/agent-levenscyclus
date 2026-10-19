"""De deterministische planner is aantoonbaar kaapbaar: de grens moet buiten het model liggen."""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from levenscyclus.agent.planner import (
    INJECTIEDOEL,
    INJECTIENOTITIE,
    LLM_OMGEVING,
    deterministische_planner,
    llm_model,
)
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


def test_llm_model_behandelt_lege_omgevingsvariabelen_als_niet_gezet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compose geeft niet-ingevulde variabelen leeg door; dan gelden de standaardwaarden."""
    pytest.importorskip("langchain_openai")
    for naam in LLM_OMGEVING:
        monkeypatch.setenv(naam, "")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    model = llm_model()
    assert type(model).__name__ == "ChatOpenAI"
    assert model.model_name == "gpt-4.1-mini"
    assert str(model.root_client.base_url) == "https://api.openai.com/v1/"


def test_llm_model_kiest_azure_met_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("langchain_openai")
    for naam in LLM_OMGEVING:
        monkeypatch.setenv(naam, "")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://voorbeeld.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "assistent")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test")
    monkeypatch.setenv("OPENAI_API_VERSION", "2024-10-21")
    model = llm_model()
    assert type(model).__name__ == "AzureChatOpenAI"
    assert model.deployment_name == "assistent"
    assert str(model.root_client.base_url).startswith(
        "https://voorbeeld.openai.azure.com/openai/deployments/assistent"
    )
