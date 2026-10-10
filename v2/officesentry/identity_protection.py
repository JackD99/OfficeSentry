"""Entra ID Protection's risky users: what the states and details mean and how to read what's stored.

``collectors/risky_users.py`` reads ``/identityProtection/riskyUsers`` (Entra ID
P2) and keeps the accounts at risk now plus those resolved in the last
``RESOLVED_DAYS`` days. The Risky users report, the account page and the check
read those rows through the helpers here.
"""

from __future__ import annotations

from .signins import OPEN_RISK, RISK_LEVELS, RISK_STATES, risk_severity
from .snapshot import TenantSnapshot

RESOLVED_DAYS = 90
RESOLVED_STATES = ("remediated", "dismissed", "confirmedSafe")

# riskDetail: why the risk is what it is (usually how it was resolved), in plain English.
DETAILS = {
    "userPerformedSecuredPasswordChange": "The person changed their password securely",
    "userPerformedSecuredPasswordReset": "The person reset their password securely",
    "adminGeneratedTemporaryPassword": "An admin set a temporary password",
    "adminConfirmedSigninSafe": "An admin confirmed the sign-in safe",
    "aiConfirmedSigninSafe": "Microsoft confirmed the sign-in safe",
    "userPassedMFADrivenByRiskBasedPolicy": "The person completed MFA when a risk policy asked",
    "adminDismissedAllRiskForUser": "An admin dismissed the risk",
    "adminConfirmedSigninCompromised": "An admin confirmed a sign-in compromised",
    "adminConfirmedUserCompromised": "An admin confirmed the account compromised",
    "userChangedPasswordOnPremises": "The person changed their password on-premises",
    "adminDismissedRiskForSignIn": "An admin dismissed the sign-in risk",
    "adminConfirmedAccountSafe": "An admin confirmed the account safe",
    "m365DAdminDismissedDetection": "Dismissed in Microsoft Defender",
    "userPerformedPasswordlessOrPhishingResistantMFA": "The person signed in with phishing-resistant MFA",
}

__all__ = [
    "DETAILS",
    "OPEN_RISK",
    "RESOLVED_DAYS",
    "RESOLVED_STATES",
    "RISK_LEVELS",
    "RISK_STATES",
    "detail",
    "level",
    "open_risk",
    "risk_severity",
    "rows",
    "severity",
    "state",
    "status",
]


def status(snap: TenantSnapshot) -> dict | None:
    """The collection status: None when never collected; ``available`` False without Entra ID P2 or consent."""
    found = snap.objects("risky_users", "risky_user_status")
    return found[0] if found else None


def rows(snap: TenantSnapshot) -> list[dict]:
    return [r for r in snap.objects("risky_users", "risky_user") or [] if isinstance(r, dict)]


def open_risk(row: dict) -> bool:
    return row.get("state") in OPEN_RISK


def level(row: dict) -> str:
    value = row.get("level")
    if value == "hidden":
        return "Hidden"
    return value.capitalize() if isinstance(value, str) and value and value != "none" else "None"


def state(row: dict) -> str:
    value = row.get("state")
    return RISK_STATES.get(value, value) if isinstance(value, str) else ""


def detail(row: dict) -> str:
    value = row.get("detail")
    if not isinstance(value, str) or value in ("none", "hidden", "unknownFutureValue"):
        return ""
    return DETAILS.get(value, value)


def severity(row: dict) -> str:
    """high, medium or low for an account at risk now."""
    return risk_severity(row.get("level"), row.get("state")) or "low"
