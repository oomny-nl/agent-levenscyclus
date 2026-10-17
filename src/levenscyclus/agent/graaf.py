"""De dossierassistent als LangGraph-graaf: planner en toolknoop in een lus.

De toolknoop doet niets bijzonders voor de beveiliging: hij roept de tool aan over de MCP-sessie.
Elk verzoek krijgt van `AgentAuth` een verse assertie; de poort beslist. Weigert de poort, dan komt
de reden terug als toolfout en de planner moet daarmee verder.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from langchain_core.messages import AnyMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from mcp.client.client import Client
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, Field

from levenscyclus.agent.planner import Planner
from levenscyclus.agent.verbinding import AgentAuth, Weigering

Waarnemer = Callable[[dict[str, Any]], None]


class Toestand(BaseModel):
    """Toestand van de graaf: alleen de berichten."""

    berichten: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)


@dataclass
class Gereedschap:
    """Wat de toolknoop nodig heeft.

    Attributes:
        client: Open MCP-sessie.
        auth: Authenticatie van die sessie (vangt de weigering op).
        waarnemer: Ontvangt gebeurtenissen voor de vertelling en de controles in de demo.
    """

    client: Client
    auth: AgentAuth
    waarnemer: Waarnemer

    async def voer_uit(
        self, naam: str, argumenten: dict[str, Any], oproep_id: str = "handmatig"
    ) -> ToolMessage:
        """Voer één toolaanroep uit; een HTTP-weigering van de poort wordt een toolfout."""
        dossier = str(argumenten.get("dossier", ""))
        extra = {"dossier": dossier}
        self.waarnemer({"soort": "aanroep", "tool": naam, "argumenten": argumenten})
        self.auth.laatste_weigering = None
        try:
            resultaat = await self.client.call_tool(naam, argumenten)
        except MCPError as fout:
            weigering = self.auth.laatste_weigering or Weigering(0, "mcp_fout", str(fout))
            tekst = f"GEWEIGERD door de poort: {weigering.code}: {weigering.reden}"
            self.waarnemer(
                {
                    "soort": "poort_weigert",
                    "tool": naam,
                    "dossier": dossier,
                    "code": weigering.code,
                    "reden": weigering.reden,
                    "status": weigering.status,
                }
            )
            return ToolMessage(
                tekst, tool_call_id=oproep_id, name=naam, status="error", additional_kwargs=extra
            )
        tekst = "\n".join(getattr(d, "text", "") for d in resultaat.content)
        fout_ = bool(resultaat.is_error)
        self.waarnemer(
            {
                "soort": "toolfout" if fout_ else "resultaat",
                "tool": naam,
                "dossier": dossier,
                "tekst": tekst,
            }
        )
        return ToolMessage(
            tekst,
            tool_call_id=oproep_id,
            name=naam,
            status="error" if fout_ else "success",
            additional_kwargs=extra,
        )


def bouw_graaf(planner: Planner, gereedschap: Gereedschap) -> Any:
    """Bouw en compileer de graaf.

    Args:
        planner: Deterministische of LLM-planner.
        gereedschap: Toolknoop-afhankelijkheden.

    Returns:
        De gecompileerde LangGraph-graaf.
    """

    def plan(state: Toestand) -> dict[str, Any]:
        return {"berichten": [planner(state.berichten)]}

    async def gereedschapsknoop(state: Toestand) -> dict[str, Any]:
        laatste = state.berichten[-1]
        antwoorden = [
            await gereedschap.voer_uit(o["name"], o["args"], str(o["id"]))
            for o in getattr(laatste, "tool_calls", [])
        ]
        return {"berichten": antwoorden}

    def verder(state: Toestand) -> str:
        return "tools" if getattr(state.berichten[-1], "tool_calls", None) else END

    graaf = StateGraph(Toestand)
    graaf.add_node("planner", plan)
    graaf.add_node("tools", gereedschapsknoop)
    graaf.add_edge(START, "planner")
    graaf.add_conditional_edges("planner", verder, {"tools": "tools", END: END})
    graaf.add_edge("tools", "planner")
    return graaf.compile()


async def toolschemas(client: Client) -> list[dict[str, Any]]:
    """Lees de tools uit de MCP-server in OpenAI-functieformaat (voor `PLANNER=llm`)."""
    lijst = await client.list_tools()
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.input_schema,
            },
        }
        for t in lijst.tools
    ]


async def voer_opdracht_uit(graaf: Any, opdracht: str) -> list[AnyMessage]:
    """Laat de agent een opdracht uitvoeren en geef de berichten terug."""
    uitkomst = await graaf.ainvoke({"berichten": [HumanMessage(opdracht)]}, {"recursion_limit": 20})
    return list(uitkomst["berichten"])
