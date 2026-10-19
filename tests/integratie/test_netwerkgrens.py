"""De demo-agent bereikt OpenFGA niet: geen route en geen preshared key.

`make integratie` geeft beide containers het interne IP van OpenFGA mee (`OPENFGA_IP`, uit
`docker inspect`). De positieve controle draait in de container `tests` (netwerken `intern` en
`front`): daar is dat adres bereikbaar. De grenstest draait in de container `demo` (alleen
netwerk `front`, `DEMO_NETWERKTEST=1`): daar faalt de naam én het adres zelf, dus de grens is
niet alleen ontbrekende DNS. De structurele controle op `compose.yaml` staat in
`tests/unit/test_compose.py`.
"""

from __future__ import annotations

import os
import socket

import httpx2
import pytest

from levenscyclus.config import Instellingen

pytestmark = pytest.mark.integratie
IN_DEMO = os.environ.get("DEMO_NETWERKTEST") == "1"
POORT_OPENFGA = 8080


def _openfga_ip() -> str:
    ip = os.environ.get("OPENFGA_IP", "")
    assert ip, "OPENFGA_IP ontbreekt: start deze test met make integratie"
    return ip


def _verbindt(ip: str) -> bool:
    """Of een TCP-verbinding met OpenFGA op dit adres lukt (time-out of geen route: nee)."""
    try:
        socket.create_connection((ip, POORT_OPENFGA), timeout=5).close()
    except OSError:
        return False
    return True


@pytest.mark.skipif(IN_DEMO, reason="positieve controle: draait in de tests-container")
def test_openfga_ip_bereikbaar_vanaf_het_interne_netwerk() -> None:
    ip = _openfga_ip()
    assert ip in {a[4][0] for a in socket.getaddrinfo("openfga", POORT_OPENFGA)}
    assert _verbindt(ip)


@pytest.mark.skipif(not IN_DEMO, reason="draait alleen in de demo-container (make integratie)")
def test_agent_bereikt_openfga_niet() -> None:
    inst = Instellingen.uit_omgeving()
    assert "OPENFGA_PRESHARED_KEY" not in os.environ
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("openfga", POORT_OPENFGA)
    with pytest.raises(httpx2.ConnectError):
        httpx2.get(f"{inst.openfga_url}/stores", timeout=5)
    # Ook het adres zelf, zonder naamresolutie: geen route van front naar intern.
    assert not _verbindt(_openfga_ip())
    # De poort zelf is wel bereikbaar: de grens is OpenFGA, niet het hele netwerk.
    assert httpx2.post(inst.poort_url, json={}, timeout=5).status_code == 401
