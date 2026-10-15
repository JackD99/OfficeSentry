"""Jinja filters for people-friendly dates and labels."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from ..formatting import capability, day, money, parse, strftime, when
from ..reports.cells import breakable, display, pdf_cell, wrappable
from ..reports.summary import possessive
from ..text import plural, subject_label

_strftime = strftime  # its old name, still used by templates' datefmt and email.py


def ago(value, now: datetime | None = None) -> str:
    """'just now', '5 minutes ago', '3 hours ago', 'yesterday', '12 days ago', or a date."""
    dt = parse(value)
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


def fromjson(value, default=None):
    try:
        return json.loads(value) if value else (default if default is not None else [])
    except ValueError:
        return default if default is not None else []


def run_error(text: str | None) -> str:
    """A failed run's error in plain words; raw Microsoft sign-in errors (from older runs) are translated."""
    from ..connection_check import explain

    if not text or text.startswith("Sign-in failed:"):
        return text or ""
    problem = explain(text)
    return f"Sign-in failed: {problem}" if problem else text


FIGURE = re.compile(r"^[-+]?[£$€]?\d[\d,.]*(%| of \d[\d,]*| [KMGT]?B)?$")


def figure_columns(rollup) -> set[str]:
    """Text columns of an All tenants table whose every value is a figure ("12", "1 of 3", "3.0 GB"),
    so they line up on the right like the number columns."""
    keys = set()
    for c in rollup.columns:
        if c.kind != "text":
            continue
        values = [r.values.get(c.key) for r in rollup.rows if not r.note]
        values = [v for v in values if v not in (None, "")]
        if values and all(isinstance(v, int | float) or FIGURE.match(str(v).strip()) for v in values):
            keys.add(c.key)
    return keys


def register(app) -> None:
    app.jinja_env.filters.update(
        ago=ago,
        when=when,
        day=day,
        datefmt=_strftime,
        capability=capability,
        money=money,
        fromjson=fromjson,
        run_error=run_error,
        plural=plural,
        possessive=possessive,  # "Northwind Traders'", not "Northwind Traders's"
        figure_columns=figure_columns,
        pdf_cell=pdf_cell,
        readable=display,
        breakable=breakable,
        wrappable=wrappable,
        subject_label=subject_label,
    )
