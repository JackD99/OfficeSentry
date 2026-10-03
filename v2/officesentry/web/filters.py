"""Jinja filters for people-friendly dates and labels."""

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


def _parse(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _strftime(dt: datetime, fmt: str) -> str:
    """strftime with ``%-d`` (day without padding) on every platform; Windows rejects ``%-d``."""
    return dt.strftime(fmt.replace("%-d", str(dt.day)))


def ago(value, now: datetime | None = None) -> str:
    """'just now', '5 minutes ago', '3 hours ago', 'yesterday', '12 days ago', or a date."""
    dt = _parse(value)
    if dt is None:
        return ""
    secs = ((now or datetime.now(UTC)) - dt).total_seconds()
    if secs < 0:
        return "in the future" if secs < -300 else "just now"
    if secs < 60:
        return "just now"
    for unit, size in (("minute", 60), ("hour", 3600)):
        n = int(secs // size)
        if n < (60 if unit == "minute" else 24):
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    days = int(secs // 86400)
    if days == 1:
        return "yesterday"
    if days < 45:
        return f"{days} days ago"
    return _strftime(dt, "%-d %b %Y")


def when(value, fmt: str = "%-d %b %Y, %H:%M UTC") -> str:
    dt = _parse(value)
    return _strftime(dt, fmt) if dt else ""


def day(value) -> str:
    return when(value, "%-d %b %Y")


def capability(key: str) -> str:
    return CAPABILITY_LABELS.get(key, key.replace("_", " ").title())


CURRENCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "AUD": "A$", "CAD": "C$", "NZD": "NZ$"}


def money(value, currency: str = "GBP", decimals: int = 0) -> str:
    if value is None:
        return ""
    return f"{CURRENCY_SYMBOLS.get(currency, currency + ' ')}{value:,.{decimals}f}"


def run_error(text: str | None) -> str:
    """A failed run's error in plain words; raw Microsoft sign-in errors (from older runs) are translated."""
    from ..connection_check import explain

    if not text or text.startswith("Sign-in failed:"):
        return text or ""
    problem = explain(text)
    return f"Sign-in failed: {problem}" if problem else text


def register(app) -> None:
    app.jinja_env.filters.update(ago=ago, when=when, day=day, datefmt=_strftime, capability=capability, money=money,
                                 run_error=run_error)
