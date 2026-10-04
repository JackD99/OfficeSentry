"""The sign-in log, summarised: what Microsoft's error codes mean and how to read the summaries.

``collectors/sign_ins.py`` reads the last ``WINDOW_DAYS`` days of interactive
sign-ins and stores summaries (per account, country, app, failure reason and
source address, and individual risky sign-ins), never the raw log. The
Sign-in activity report, the account page and the risky sign-in check read
those summaries through the helpers here, so the meaning of a code or what
counts as "unusual" is decided in one place and applies to stored data too.
"""

from __future__ import annotations

from .snapshot import TenantSnapshot

WINDOW_DAYS = 7
REPEATED_FAILURES = 5  # failed sign-ins in the window that make an account worth a look
SPRAY_ACCOUNTS = 5  # one address failing against this many accounts looks like password spraying
RARE_SHARE = 0.05  # a country with less than this share of successful sign-ins is unusual for the tenant

# Codes that mean the person was asked for something (MFA, "Stay signed in?") rather than refused.
# The sign-in log records these as failures, but they're part of a normal sign-in.
INTERRUPTS = {
    16000: "Asked to pick an account",
    16001: "Asked to pick an account",
    50058: "No session to sign in silently; the person was asked to sign in",
    50072: "Asked to register for MFA",
    50074: "Asked to complete MFA",
    50076: "Asked to complete MFA (policy, new location or new app)",
    50079: "Asked to register for MFA",
    50097: "Asked for device authentication",
    50125: "Interrupted by a password reset or registration prompt",
    50140: "Interrupted by the \"Stay signed in?\" prompt",
    50199: "Interrupted by a security confirmation prompt",
    81010: "Seamless single sign-on didn't work; the person was asked to sign in",
}
FAILURES = {
    50005: "Blocked: the device platform isn't supported by Conditional Access",
    50020: "The account from another organisation isn't a guest in this tenant",
    50034: "No such account in this tenant",
    50053: "Locked out: too many wrong passwords (smart lockout), or the address is known to be malicious",
    50055: "Password expired",
    50056: "Wrong password (the account has no password set)",
    50057: "Account is disabled",
    50059: "No tenant found for the account's domain",
    50089: "Took too long: the sign-in page expired",
    50105: "Not assigned to this application",
    50126: "Wrong user name or password",
    50131: "Blocked: suspicious activity or device state",
    50132: "Session ended by a password change or sign-out everywhere",
    50133: "Session ended by a password change",
    50135: "Password change required because the account looks risky",
    50142: "Password change required by Conditional Access",
    50144: "On-premises password expired",
    50158: "Third-party MFA (external security challenge) not completed",
    50173: "Session ended by a password change or sign-out everywhere",
    53000: "Blocked by Conditional Access: the device isn't compliant",
    53001: "Blocked by Conditional Access: the device isn't joined to the domain",
    53002: "Blocked by Conditional Access: the app isn't approved",
    53003: "Blocked by Conditional Access",
    53004: "Can't register for MFA because the sign-in looks risky",
    65001: "The app needs consent first",
    70043: "Session expired under a sign-in frequency policy",
    90072: "The account isn't a guest in this tenant",
    90094: "The app needs admin consent",
    90095: "The app needs admin consent",
    500121: "MFA not completed: the person didn't approve or answer the MFA request",
    7000112: "The application is disabled",
}
# Wrong or locked password: what password guessing produces.
CREDENTIAL_CODES = {50126, 50053, 50056}
LOCKOUT = 50053
RISK_LEVELS = ("low", "medium", "high")
OPEN_RISK = ("atRisk", "confirmedCompromised")
RISK_STATES = {"atRisk": "At risk", "confirmedCompromised": "Confirmed compromised", "remediated": "Remediated",
               "dismissed": "Dismissed", "confirmedSafe": "Confirmed safe", "none": "None"}


def code(value) -> int:
    """An error code as a number; 0 (success) when it's missing or unreadable."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return -1


def outcome(error_code: int) -> str:
    """success, interrupted (an MFA or other prompt) or failure."""
    if error_code == 0:
        return "success"
    return "interrupted" if error_code in INTERRUPTS else "failure"


def meaning(error_code, reason=None) -> str:
    """What a sign-in error code means, in plain English; Microsoft's own text for codes not listed."""
    n = code(error_code)
    if n == 0:
        return "Signed in"
    text = INTERRUPTS.get(n) or FAILURES.get(n)
    if text:
        return text
    if isinstance(reason, str) and reason.strip() and reason.strip().lower() != "other.":
        return reason.strip()
    return f"Error {n}" if n > 0 else "Unknown error"


def risk_severity(level, state) -> str | None:
    """high, medium or None for a risky sign-in, by Entra ID Protection's level and state."""
    if state == "confirmedCompromised" or (level == "high" and state in OPEN_RISK):
        return "high"
    if level == "medium" and state in OPEN_RISK:
        return "medium"
    return None


def country(value) -> str:
    return value if isinstance(value, str) and value else "Unknown"


# -- Reading the stored summaries --------------------------------------------------------------


def status(snap: TenantSnapshot) -> dict | None:
    """The collection status: None when never collected; ``available`` False without Entra ID P1."""
    rows = snap.objects("sign_ins", "sign_in_status")
    return rows[0] if rows else None


def _num(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def unusual_countries(snap: TenantSnapshot) -> set[str]:
    """Countries the tenant rarely signs in from: under ``RARE_SHARE`` of successful sign-ins, and
    neither the organisation's own country nor the one most sign-ins come from."""
    rows = [r for r in snap.objects("sign_ins", "sign_in_country") or [] if isinstance(r.get("country"), str)]
    total = sum(_num(r.get("succeeded")) for r in rows)
    if not total:
        return set()
    home = {o.get("countryLetterCode") for o in snap.objects("tenant_profile", "organization") or []}
    top = max(rows, key=lambda r: _num(r.get("succeeded")))["country"]
    return {r["country"] for r in rows
            if _num(r.get("succeeded")) and r["country"] not in home and r["country"] != top
            and _num(r.get("succeeded")) / total < RARE_SHARE}


def for_account(snap: TenantSnapshot, user_id: str, names: set[str]) -> dict | None:
    """One account's summary row, by object ID or sign-in name (lower case)."""
    for r in snap.objects("sign_ins", "sign_in_account") or []:
        user = r.get("user")
        if r.get("userId") == user_id or (isinstance(user, str) and user.lower() in names):
            return r
    return None
