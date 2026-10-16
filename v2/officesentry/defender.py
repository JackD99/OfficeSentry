"""Microsoft Defender alerts and incidents as stored by ``collectors/security_alerts.py``, for the Security alerts
report and its check: what each alert is about in plain words, and whether someone is dealing with it.

Defender gathers alerts from the services that report to it (Defender for Office 365, Endpoint, Identity and
Cloud Apps, Entra ID Protection, data loss prevention and others), and groups related ones into incidents. Reads
stored snapshots only.
"""

from __future__ import annotations

import re

from .snapshot import TenantSnapshot

ALERTS = "SecurityAlert.Read.All"
INCIDENTS = "SecurityIncident.Read.All"
PARTS = {"alerts": ALERTS, "incidents": INCIDENTS}
DAYS = 30  # resolved alerts and incidents are kept for this long; open ones for as long as Defender keeps them

SEVERITIES = {"high": "High", "medium": "Medium", "low": "Low", "informational": "Informational"}
# A finding's flag in a report, by Defender's severity: informational ones aren't flagged.
FLAGS = {"high": "high", "medium": "medium", "low": "low"}
ALERT_STATES = {"new": "New", "inProgress": "In progress", "resolved": "Resolved"}
INCIDENT_STATES = {
    "active": "Active",
    "inProgress": "In progress",
    "awaitingAction": "Awaiting action",
    "resolved": "Resolved",
    "redirected": "Merged into another",
}
CLASSIFICATIONS = {
    "truePositive": "Real threat",
    "falsePositive": "False alarm",
    "informationalExpectedActivity": "Expected activity",
}
SOURCES = {
    "microsoftDefenderForEndpoint": "Defender for Endpoint",
    "microsoftDefenderForIdentity": "Defender for Identity",
    "microsoftDefenderForCloudApps": "Defender for Cloud Apps",
    "microsoftDefenderForOffice365": "Defender for Office 365",
    "microsoft365Defender": "Defender XDR",
    "azureAdIdentityProtection": "Entra ID Protection",
    "microsoftAppGovernance": "App governance",
    "dataLossPrevention": "Data loss prevention",
    "microsoftDefenderForCloud": "Defender for Cloud",
    "microsoftSentinel": "Microsoft Sentinel",
    "microsoftInsiderRiskManagement": "Insider risk management",
}
OPEN_INCIDENT = ("active", "inProgress", "awaitingAction")
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "informational": 3}


def words(value: str | None) -> str:
    """Defender's camelCase values in words: ``compromisedAccount`` -> "Compromised account"."""
    if not isinstance(value, str) or not value or value in ("unknown", "unknownFutureValue"):
        return ""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value).lower()
    return spaced[0].upper() + spaced[1:]


def status(snap: TenantSnapshot) -> dict | None:
    """How the latest collection read each part ("alerts", "incidents": "read", "waiting" for the client to approve
    the permission, "unavailable" when Defender refused for this tenant, or "failed"), or None when it hasn't run."""
    rows = snap.objects("security_alerts", "security_alert_status")
    if rows is None:
        return None
    return rows[0] if rows and isinstance(rows[0], dict) else {}


def alerts(snap: TenantSnapshot) -> list[dict]:
    return [r for r in snap.objects("security_alerts", "security_alert") or [] if isinstance(r, dict)]


def incidents(snap: TenantSnapshot) -> list[dict]:
    return [r for r in snap.objects("security_alerts", "security_incident") or [] if isinstance(r, dict)]


def alert_open(alert: dict) -> bool:
    """An alert nobody has resolved yet."""
    return alert.get("status") != "resolved"


def incident_open(incident: dict) -> bool:
    """An incident still being dealt with (one merged into another isn't)."""
    return incident.get("status") in OPEN_INCIDENT


def untouched(alert: dict) -> bool:
    """An open alert nobody has picked up: still new and assigned to nobody."""
    return alert.get("status") in ("new", None) and not alert.get("assignedTo")


def ordered(rows: list[dict], is_open) -> list[dict]:
    """Open first, then the most severe, then the newest."""
    newest_first = sorted(rows, key=lambda r: r["created"] if isinstance(r.get("created"), str) else "", reverse=True)
    return sorted(newest_first, key=lambda r: (not is_open(r), SEVERITY_ORDER.get(r.get("severity"), 4)))
