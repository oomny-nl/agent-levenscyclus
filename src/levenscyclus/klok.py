"""Registerklok: echte tijd plus een offset die alleen in demostand vooruit kan.

Alle levenscyclusregels, `beoordeeldOp`, de `huidige_tijd` voor OpenFGA-condities en de tijdstempels
in het logboek gebruiken deze klok. Agent-asserties gebruiken de echte klok (zie poort).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def parse_tijd(tekst: str) -> datetime:
    """Zet een SCIM/ISO 8601-dateTime om in een tijdzonebewuste `datetime` (UTC).

    Raises:
        ValueError: Bij een onleesbare of tijdzoneloze waarde.
    """
    waarde = datetime.fromisoformat(tekst.replace("Z", "+00:00"))
    if waarde.tzinfo is None:
        raise ValueError(f"tijdstip {tekst!r} heeft geen tijdzone; gebruik bijvoorbeeld ...Z")
    return waarde.astimezone(UTC)


def iso(tijd: datetime) -> str:
    """Tijd als ISO 8601 in UTC met `Z`, op milliseconden."""
    return tijd.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Klok:
    """Klok met een vooruit te zetten offset.

    Args:
        demostand: Alleen dan mag de offset veranderen.
    """

    def __init__(self, demostand: bool = False) -> None:
        self.demostand = demostand
        self._offset = timedelta(0)

    @property
    def offset(self) -> timedelta:
        """Huidige offset ten opzichte van de echte tijd."""
        return self._offset

    def nu(self) -> datetime:
        """Registertijd (UTC)."""
        return datetime.now(UTC) + self._offset

    def herstel(self, offset: timedelta) -> None:
        """Zet de offset terug na een herstart, zodat de registertijd niet terugspringt.

        Raises:
            PermissionError: Bij een offset buiten de demostand.
        """
        if offset and not self.demostand:
            raise PermissionError(
                "het logboek bevat demoklokstappen, maar DEMO_KLOK staat uit; start met "
                "DEMO_KLOK=1 of met een leeg register"
            )
        self._offset = offset

    def zet_vooruit(self, duur: timedelta) -> datetime:
        """Zet de klok vooruit; nooit terug.

        Raises:
            PermissionError: Buiten de demostand.
            ValueError: Bij een duur van nul of negatief.
        """
        if not self.demostand:
            raise PermissionError("de registerklok kan alleen met DEMO_KLOK=1 worden verzet")
        if duur <= timedelta(0):
            raise ValueError("de klok gaat alleen vooruit")
        self._offset += duur
        return self.nu()
