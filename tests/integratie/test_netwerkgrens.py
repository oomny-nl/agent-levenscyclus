"""De demo-agent bereikt OpenFGA niet: geen route en geen preshared key.

Draait in de container `demo` (alleen netwerk `front`), niet in `tests`; `make integratie` start
hem met `DEMO_NETWERKTEST=1`. De structurele controle op `compose.yaml` staat in
`tests/unit/test_compose.py`.
"""

from __future__ import annotations

import os
import socket

import httpx2
import pytest

from levenscyclus.config import Instellingen

pytestmark = [
    pytest.mark.integratie,
    pytest.mark.skipif(
        os.environ.get("DEMO_NETWERKTEST") != "1",
        reason="draait alleen in de demo-container (make integratie)",
    ),
]


def test_agent_bereikt_openfga_niet() -> None:
    inst = Instellingen.uit_omgeving()
    assert "OPENFGA_PRESHARED_KEY" not in os.environ
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("openfga", 8080)
    with pytest.raises(httpx2.ConnectError):
        httpx2.get(f"{inst.openfga_url}/stores", timeout=5)
    # De poort zelf is wel bereikbaar: de grens is OpenFGA, niet het hele netwerk.
    assert httpx2.post(inst.poort_url, json={}, timeout=5).status_code == 401
