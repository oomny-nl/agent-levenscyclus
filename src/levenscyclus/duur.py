"""ISO 8601-duren voor termijnen in het SCIM-record (`P90D`, `P14D`, `PT2S`).

Ondersteund: weken, dagen, uren, minuten en seconden; jaren en maanden niet (hun lengte hangt
van de kalender af). Dat is genoeg voor herbeoordelings- en bevestigingstermijnen.
"""

from __future__ import annotations

import re
from datetime import timedelta

_DUUR = re.compile(
    r"^P(?!$)(?:(?P<w>\d+)W)?(?:(?P<d>\d+)D)?"
    r"(?:T(?!$)(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?$"
)


def parse_duur(tekst: str) -> timedelta:
    """Zet een ISO 8601-duur om in een `timedelta`.

    Args:
        tekst: Bijvoorbeeld `P90D`, `P2W`, `PT36H`, `PT2S` of `P1DT12H`.

    Returns:
        De duur.

    Raises:
        ValueError: Bij een lege, negatieve, kalenderafhankelijke (jaren, maanden) of misvormde
            duur.
    """
    gevonden = _DUUR.match(tekst.strip())
    if gevonden is None:
        raise ValueError(
            f"ongeldige ISO 8601-duur {tekst!r}; gebruik bijvoorbeeld P90D, P2W of PT2S "
            "(jaren en maanden worden niet ondersteund)"
        )
    delen = {k: float(v) for k, v in gevonden.groupdict().items() if v is not None}
    duur = timedelta(
        weeks=delen.get("w", 0),
        days=delen.get("d", 0),
        hours=delen.get("h", 0),
        minutes=delen.get("m", 0),
        seconds=delen.get("s", 0),
    )
    if duur <= timedelta(0):
        raise ValueError(f"duur {tekst!r} moet groter dan nul zijn")
    return duur
