"""Apps Intune found on managed devices (``collectors/detected_apps.py``): remote access tools among them,
and versions in the order people expect."""

from __future__ import annotations

import re

from .snapshot import TenantSnapshot

# Tools that let someone control a computer from elsewhere. MSPs use some of them, and attackers install them
# too (ransomware groups favour AnyDesk, ScreenConnect and the like), so each one found is worth checking.
REMOTE_TOOLS = {
    "Action1": r"\baction1\b",
    "AeroAdmin": r"\baeroadmin\b",
    "Ammyy Admin": r"\bammyy\b",
    "AnyDesk": r"\banydesk\b",
    "Atera": r"\batera agent\b",
    "BeyondTrust Remote Support": r"\bbeyondtrust (?:remote support|jump client)\b|\bbomgar\b",
    "Chrome Remote Desktop": r"\bchrome remote desktop\b",
    "DWService": r"\bdwagent\b|\bdwservice\b",
    "GoTo (LogMeIn)": r"\blogmein\b|\bgoto ?(?:resolve|assist|opener)\b",
    "ISL Online": r"\bisl (?:light|alwayson)\b",
    "MeshCentral": r"\bmesh ?agent\b",
    "NetSupport": r"\bnetsupport\b",
    "Parsec": r"\bparsec\b",
    "Pulseway": r"\bpulseway\b",
    "Radmin": r"\bradmin\b",
    "RemotePC": r"\bremotepc\b",
    "Remote Utilities": r"\bremote utilities\b",
    "RustDesk": r"\brustdesk\b",
    "ScreenConnect": r"\bscreenconnect\b|\bconnectwise control\b",
    "SimpleHelp": r"\bsimplehelp\b|\bsimple-help\b",
    "Splashtop": r"\bsplashtop\b",
    "Supremo": r"\bsupremo\b",
    "TeamViewer": r"\bteamviewer\b",
    "UltraViewer": r"\bultraviewer\b",
    "VNC": r"\b(?:real|tight|ultra|tiger)vnc\b|\bvnc (?:server|viewer|connect)\b",
    "Zoho Assist": r"\bzoho assist\b",
}
_TOOLS = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in REMOTE_TOOLS.items()]

PLATFORMS = {
    "windows": "Windows",
    "macos": "macOS",
    "ios": "iOS",
    "chromeos": "ChromeOS",
    "windowsholographic": "HoloLens",
}


def remote_tool(name: str | None) -> str | None:
    """Which remote access tool an app's name is, if any."""
    if not name:
        return None
    return next((tool for tool, pattern in _TOOLS if pattern.search(name)), None)


def platform(value: str | None) -> str:
    if not value or value == "unknown":
        return ""
    if value.lower().startswith("android"):
        return "Android"
    return PLATFORMS.get(value.lower(), value)


def version_key(version: str | None) -> tuple:
    """Sorts "10.2" after "9.11", as people read versions."""
    parts = re.split(r"[^0-9A-Za-z]+", version or "")
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in parts if p)


def apps(snap: TenantSnapshot) -> list[dict] | None:
    rows = snap.objects("detected_apps", "detected_app")
    return None if rows is None else [r for r in rows if isinstance(r, dict) and r.get("name")]


def status(snap: TenantSnapshot) -> dict | None:
    rows = snap.objects("detected_apps", "detected_app_status")
    return rows[0] if rows and isinstance(rows[0], dict) else None
