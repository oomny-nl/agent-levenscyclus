"""Structurele netwerkgrens in `compose.yaml`: de agent heeft geen weg naar OpenFGA."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "compose.yaml"


def _diensten() -> dict[str, Any]:
    return dict(yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"])


def test_compose_scheidt_agent_van_openfga() -> None:
    diensten = _diensten()
    assert diensten["openfga"]["networks"] == ["intern"]
    assert diensten["demo"]["networks"] == ["front"]
    assert "OPENFGA_PRESHARED_KEY" not in diensten["demo"].get("environment", {})
    assert "openfga" not in diensten["demo"].get("depends_on", {})
    # Alleen register, poort en tests delen het interne netwerk met OpenFGA.
    intern = {n for n, d in diensten.items() if "intern" in d.get("networks", [])}
    assert intern == {"openfga", "register", "poort", "tests"}
    assert "ports" not in diensten["openfga"]
