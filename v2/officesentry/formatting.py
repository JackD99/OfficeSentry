"""Numbers, money, dates and labels as people read them.

Shared by the web pages (as Jinja filters, ``web/filters.py``), the reports and
the emails, so reports never import from ``web``.
"""

from __future__ import annotations

from datetime import UTC, datetime

CAPABILITY_LABELS = {
    "entra_p1": "Entra ID P1",
    "entra_p2": "Entra ID P2",
    "intune": "Intune",
    "defender_o365_p1": "Defender for Office 365 P1",
    "defender_o365_p2": "Defender for Office 365 P2",
    "exchange": "Exchange Online",
    "sharepoint": "SharePoint Online",
}


def parse(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


NO_PAD_DAY = "%-d"  # the day without padding, which Windows' own strftime rejects


def strftime(dt: datetime, fmt: str) -> str:
    """strftime with ``%-d`` (day without padding) on every platform; Windows rejects ``%-d``."""
    return dt.strftime(fmt.replace(NO_PAD_DAY, str(dt.day)))


def when(value, fmt: str = "%-d %b %Y, %H:%M UTC") -> str:
    dt = parse(value)
    return strftime(dt, fmt) if dt else ""


def day(value) -> str:
    return when(value, "%-d %b %Y")


def capability(key: str) -> str:
    return CAPABILITY_LABELS.get(key, key.replace("_", " ").title())


CURRENCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "AUD": "A$", "CAD": "C$", "NZD": "NZ$"}


def money(value, currency: str = "GBP", decimals: int = 0) -> str:
    if value is None:
        return ""
    return f"{CURRENCY_SYMBOLS.get(currency, currency + ' ')}{value:,.{decimals}f}"
