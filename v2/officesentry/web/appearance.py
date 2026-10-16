"""How the portal looks to each person (Account, Appearance): light, dark or the device's setting, and for
staff one of the colour themes in ``static/themes.css``. Stored on the person (``users.theme``,
``users.accent``, migration 0047) and drawn by ``base.html`` as ``data-theme`` and ``data-accent`` on
``<html>``. Only their own screen changes: client reports, PDFs and emails never read it."""

from __future__ import annotations

from dataclasses import dataclass

# Light or dark: "system" follows the device (no data-theme, so tokens.css's media query decides).
MODES = {
    "system": ("Same as this device", "Light or dark as your computer or phone is set."),
    "light": ("Light", ""),
    "dark": ("Dark", ""),
}
# Colour themes, in the order the Account page offers them; the first is the default. Each is a block in
# themes.css, and only the accent and the menu change.
THEMES = {
    "green": "Bottle green",
    "navy": "Navy",
    "claret": "Claret",
    "graphite": "Graphite",
    "plum": "Plum",
}
DEFAULT_MODE = "system"
DEFAULT_THEME = "green"


@dataclass(frozen=True)
class Look:
    theme: str  # "light", "dark" or "" (follow the device)
    accent: str  # a key of THEMES, or "" for the default


def mode_of(user) -> str:
    value = _get(user, "theme")
    return value if value in MODES else DEFAULT_MODE


def theme_of(user) -> str:
    value = _get(user, "accent")
    return value if value in THEMES else DEFAULT_THEME


def look(user) -> Look:
    """What ``base.html`` puts on ``<html>`` for this person: nothing for the defaults or nobody signed in.
    Colour themes are for staff; a client account keeps the portal's own colours."""
    if user is None:
        return Look("", "")
    mode = mode_of(user)
    accent = theme_of(user) if user["role"] != "client" else DEFAULT_THEME
    return Look("" if mode == DEFAULT_MODE else mode, "" if accent == DEFAULT_THEME else accent)


def _get(user, column: str) -> str:
    try:
        return user[column] or ""
    except (IndexError, KeyError, TypeError):
        return ""
