"""Risky users: accounts Entra ID Protection thinks someone else may be using.

Reads what ``collectors/risky_users.py`` stores: the accounts at risk now, then
those resolved recently (and how). Needs Entra ID P2.
"""

from __future__ import annotations

from datetime import datetime

from .. import clouds
from .. import identity_protection as ip
from ..snapshot import TenantSnapshot
from ..text import plural
from .data_reports import DataReport, Rows, Section, Tile
from .registry import report

NOT_COLLECTED = 'Risky users haven\'t been collected yet. Refresh "Risky users" on the tenant page.'
NO_P2 = (
    "Risky users come from Entra ID Protection, which needs Entra ID P2 (included in Microsoft 365 E5, or as "
    "an add-on to Business Premium or E3). This tenant doesn't have it. The Sign-in activity report still "
    "shows failed sign-ins, unusual countries and sign-ins without MFA."
)
NO_PERMISSION = (
    "Office Sentry can't read this tenant's risky users. It needs the IdentityRiskyUser.Read.All "
    "permission: open the tenant page, send the consent link to the client's Global Admin again and "
    "ask them to accept."
)

AT_RISK = "At risk now"
RESOLVED = "Recently resolved"


def _utc(value) -> str:
    return value[:16].replace("T", " ") if isinstance(value, str) and len(value) >= 16 else ""


@report(
    "risky-users",
    "Risky users",
    "Accounts Entra ID Protection rates at risk or confirmed compromised, and the risk resolved "
    "recently (needs Entra ID P2).",
    ("risky_users", "tenant_profile"),
    group="identity",
    order=70,
    keywords="identity protection risk compromised hacked breach leaked credentials at risk p2 remediated dismissed",
)
def risky_users_report(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    if why := clouds.missing(clouds.of(snap.tenant), "risky_users"):
        report.unavailable = why
        return
    status = ip.status(snap)
    if status is None:
        report.unavailable = NOT_COLLECTED
        return
    if not status.get("available"):
        report.unavailable = NO_PERMISSION if status.get("reason") == "permission" else NO_P2
        return
    days = status.get("days") if isinstance(status.get("days"), int) else ip.RESOLVED_DAYS
    rows = ip.rows(snap)
    newest = lambda r: r.get("updated") if isinstance(r.get("updated"), str) else ""  # noqa: E731

    at_risk = Section(
        AT_RISK,
        ["Account", "Name", "Risk level", "Risk state", "Detail", "Account deleted", "Last updated (UTC)"],
        intro="Accounts Entra ID Protection thinks someone else may be using. Check each with the "
        "person. If they don't recognise recent sign-ins, reset the password, sign them out "
        "everywhere (revoke sessions) and check their MFA methods, inbox rules and app "
        "consents, then confirm the account compromised or dismiss the risk in Entra ID "
        "Protection so it leaves this list.",
        empty="No accounts are at risk.",
    )
    open_rows = sorted(
        (r for r in rows if ip.open_risk(r)),
        key=lambda r: (r.get("deleted") is True, {"high": 0, "medium": 1}.get(ip.severity(r), 2), newest(r)),
    )
    for r in open_rows:
        at_risk.add(
            [
                r.get("user"),
                r.get("displayName"),
                ip.level(r),
                ip.state(r),
                ip.detail(r),
                r.get("deleted") is True,
                _utc(r.get("updated")),
            ],
            "low" if r.get("deleted") is True else ip.severity(r),
        )

    resolved = Section(
        RESOLVED,
        ["Account", "Name", "Risk level", "Outcome", "How", "Resolved (UTC)"],
        compare=False,
        intro=f"Accounts whose risk was remediated, dismissed or confirmed safe in the last {days} "
        "days. A password reset or MFA prompt by a risk policy remediates automatically; "
        "dismissed risk is an admin's decision that it wasn't the person's account being "
        "misused.",
        empty=f"No risk was resolved in the last {days} days.",
    )
    for r in sorted((r for r in rows if not ip.open_risk(r)), key=newest, reverse=True):
        resolved.add(
            [r.get("user"), r.get("displayName"), ip.level(r), ip.state(r), ip.detail(r), _utc(r.get("updated"))]
        )
    report.sections = [at_risk, resolved]

    live = [r for r in open_rows if r.get("deleted") is not True]
    high = sum(r.get("level") == "high" for r in live)
    compromised = sum(r.get("state") == "confirmedCompromised" for r in live)
    report.tiles = [
        Tile(
            "At risk",
            len(live),
            "Accounts Entra ID Protection rates at risk"
            if len(live) == len(at_risk.rows)
            else f"Not counting {plural(len(at_risk.rows) - len(live), 'deleted account')}",
            ("bad" if high or compromised else "warn") if live else "ok",
            Rows(AT_RISK, {"Account deleted": "No"}),
        ),
        Tile(
            "High risk",
            high,
            "At risk with a high risk level",
            "bad" if high else "ok",
            Rows(AT_RISK, {"Risk level": "High", "Account deleted": "No"}),
        ),
        Tile(
            "Confirmed compromised",
            compromised,
            "An admin confirmed someone else had the account",
            "bad" if compromised else "ok",
            Rows(AT_RISK, {"Risk state": "Confirmed compromised", "Account deleted": "No"}),
        ),
        Tile(
            "Recently resolved",
            len(resolved.rows),
            f"Remediated, dismissed or confirmed safe, last {days} days",
            rows=Rows(RESOLVED),
        ),
    ]
    report.footnote = (
        "From Entra ID Protection's risky users. A user's risk combines every risky sign-in and "
        "detection (leaked credentials, unusual activity) Microsoft has linked to the account. "
        "Times are UTC."
    )
    if status.get("truncated"):
        report.notes.append(f"Only the first {status.get('read', 0):,} risky users were read.")
