"""Security alerts: what Microsoft Defender has flagged, how severe, and whether anyone has picked it up.

Reads what ``collectors/security_alerts.py`` stores: Defender's incidents still open, its alerts still open, and
both resolved recently, with the accounts and devices each alert names. The alerts come from every service that
reports to Defender (Defender for Office 365 and Endpoint, Entra ID Protection, data loss prevention and others),
so what a tenant has depends on its licences.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from .. import clouds
from .. import defender as d
from ..permission_batch import waiting_text
from ..snapshot import TenantSnapshot
from .data_reports import DataReport, Link, Rows, Section, Tile, metric_trend
from .registry import report

NOT_COLLECTED = 'Security alerts haven\'t been collected yet. Refresh "Security alerts" on the client page.'
NO_DEFENDER = (
    "Microsoft Defender has no alerts to give for this tenant: none of the services that report to it is "
    "set up. Microsoft 365 Business Premium, Defender for Business and Defender for Office 365 add them."
)

INCIDENTS = "Open incidents"
ALERTS = "Open alerts"
RESOLVED = "Recently resolved"


def _utc(value) -> str:
    return value[:16].replace("T", " ") if isinstance(value, str) and len(value) >= 16 else ""


def _link(url) -> Link | str:
    return Link("Open in Defender", url) if isinstance(url, str) and url else ""


def _outcome(row: dict) -> str:
    """What whoever closed it said it was: "Real threat (phishing)", "False alarm", or Defender's own words."""
    said = d.CLASSIFICATIONS.get(row.get("classification") or "", "")
    why = d.words(row.get("determination"))
    return f"{said} ({why.lower()})" if said and why else said or why


def _picked_up(alert: dict) -> bool:
    return not d.untouched(alert)


@report(
    "security-alerts",
    "Security alerts",
    "What Microsoft Defender has flagged: open incidents and alerts, how severe, who is dealing with each, the "
    "accounts and devices they name, and what was resolved recently.",
    ("security_alerts",),
    group="tenant",
    order=15,
    keywords="defender alerts incidents xdr threats malware phishing compromise suspicious activity soc "
    "security operations endpoint office 365 data loss prevention dlp investigate",
    uses=("compromise",),
)
def security_alerts_report(snap: TenantSnapshot, now: datetime, out: DataReport) -> None:
    if why := clouds.missing(clouds.of(snap.tenant), "security_alerts"):
        out.unavailable = why
        return
    status = d.status(snap)
    if status is None:
        out.unavailable = NOT_COLLECTED
        return
    parts = status.get("parts") if isinstance(status.get("parts"), dict) else {}
    out.parts = {k: v for k, v in parts.items() if isinstance(v, str)}
    if parts and all(v == "unavailable" for v in parts.values()):
        out.unavailable = NO_DEFENDER
        return
    waiting = [d.PARTS[p] for p, state in parts.items() if state == "waiting" and p in d.PARTS]
    if waiting:
        out.notice = waiting_text(snap.tenant["name"], waiting)
    days = status.get("days") if isinstance(status.get("days"), int) else d.DAYS

    def missing(part: str, what: str) -> str:
        state = parts.get(part)
        if state == "waiting":
            return f"{what} will show once the client approves the new permission."
        if state == "unavailable":
            return f"Microsoft Defender has no {what.lower()} to give for this tenant."
        return f"{what} couldn't be read; see the Security alerts collection's log."

    alerts = d.ordered(d.alerts(snap), d.alert_open)
    incidents = d.ordered(d.incidents(snap), d.incident_open)
    per_incident = Counter(a["incident"] for a in alerts if a.get("incident"))
    open_alerts = [a for a in alerts if d.alert_open(a)]

    incident_table = Section(
        INCIDENTS,
        ["Incident", "Severity", "Status", "Assigned to", "Alerts", "Created (UTC)", "Last updated (UTC)", "Link"],
        intro="Defender groups related alerts into an incident, so one attack shows once. Work through these "
        "from the most severe: assign each to someone, and close it in Defender with what it turned out to be.",
        empty="No incidents are open." if parts.get("incidents") == "read" else missing("incidents", "Incidents"),
        optional=("Last updated (UTC)",),
    )
    for i in (i for i in incidents if d.incident_open(i)):
        incident_table.add(
            [
                i.get("name"),
                d.SEVERITIES.get(i.get("severity") or "", d.words(i.get("severity"))),
                d.INCIDENT_STATES.get(i.get("status") or "", d.words(i.get("status"))),
                i.get("assignedTo") or "",
                per_incident.get(str(i.get("_id")), 0) if parts.get("alerts") == "read" else "",
                _utc(i.get("created")),
                _utc(i.get("updated")),
                _link(i.get("url")),
            ],
            d.FLAGS.get(i.get("severity") or ""),
        )

    alert_table = Section(
        ALERTS,
        [
            "Alert",
            "Severity",
            "Status",
            "Picked up",
            "Source",
            "Category",
            "Accounts",
            "Devices",
            "Assigned to",
            "Created (UTC)",
            "Link",
        ],
        intro="Alerts nobody has resolved yet. One that's still new and assigned to nobody hasn't been looked "
        "at. Check what each names: for an account, its recent sign-ins, inbox rules and MFA methods; for a "
        "device, what Defender found on it. Then resolve it in Defender, saying whether it was a real threat.",
        empty="No alerts are open." if parts.get("alerts") == "read" else missing("alerts", "Alerts"),
        optional=("Category", "Assigned to"),
    )
    for a in open_alerts:
        flag = d.FLAGS.get(a.get("severity") or "")
        alert_table.add(
            [
                a.get("title"),
                d.SEVERITIES.get(a.get("severity") or "", d.words(a.get("severity"))),
                d.ALERT_STATES.get(a.get("status") or "", d.words(a.get("status"))),
                _picked_up(a),
                d.SOURCES.get(a.get("source") or "", d.words(a.get("source"))),
                d.words(a.get("category")),
                a.get("users") or [],
                a.get("devices") or [],
                a.get("assignedTo") or "",
                _utc(a.get("created")),
                _link(a.get("url")),
            ],
            flag,
        )

    resolved = Section(
        RESOLVED,
        ["Alert", "Severity", "Source", "Outcome", "Accounts", "Devices", "Resolved (UTC)"],
        compare=False,
        intro=f"Alerts resolved in the last {days} days, and what whoever closed each said it was.",
        empty=f"No alerts were resolved in the last {days} days."
        if parts.get("alerts") == "read"
        else missing("alerts", "Alerts"),
    )
    closed = [a for a in alerts if not d.alert_open(a)]
    for a in sorted(closed, key=lambda a: a.get("resolved") or a.get("updated") or "", reverse=True):
        resolved.add(
            [
                a.get("title"),
                d.SEVERITIES.get(a.get("severity") or "", d.words(a.get("severity"))),
                d.SOURCES.get(a.get("source") or "", d.words(a.get("source"))),
                _outcome(a),
                a.get("users") or [],
                a.get("devices") or [],
                _utc(a.get("resolved") or a.get("updated")),
            ]
        )
    out.sections = [incident_table, alert_table, resolved]

    if parts.get("alerts") == "read":
        high = sum(a.get("severity") == "high" for a in open_alerts)
        untouched = sum(not _picked_up(a) for a in open_alerts)
        out.tiles += [
            Tile(
                "Open alerts",
                len(open_alerts),
                "Not resolved in Defender",
                ("bad" if high else "warn") if open_alerts else "ok",
                Rows(ALERTS),
                metric_trend(snap, "defender.alerts_open", now),
            ),
            Tile(
                "High severity",
                high,
                "Open alerts Defender rates high",
                "bad" if high else "ok",
                Rows(ALERTS, {"Severity": "High"}),
            ),
            Tile(
                "Not picked up",
                untouched,
                "Still new and assigned to nobody",
                "warn" if untouched else "ok",
                Rows(ALERTS, {"Picked up": "No"}),
            ),
        ]
    if parts.get("incidents") == "read":
        out.tiles.append(
            Tile(
                "Open incidents",
                len(incident_table.rows),
                "Related alerts grouped by Defender",
                "warn" if incident_table.rows else "ok",
                Rows(INCIDENTS),
            )
        )
    if parts.get("alerts") == "read":
        out.tiles.append(
            Tile("Recently resolved", len(resolved.rows), f"Alerts resolved, last {days} days", rows=Rows(RESOLVED))
        )

    out.footnote = (
        "From Microsoft Defender's alerts and incidents. Which services report to it depends on the tenant's "
        "licences: Defender for Office 365 for email, Defender for Endpoint or Business for computers, Entra ID "
        "Protection for sign-ins. Office Sentry keeps the accounts and devices an alert names, never the email, "
        "file or command behind it. Times are UTC."
    )
    for part, what in (("alerts", "alerts"), ("incidents", "incidents")):
        if status.get(f"{part}_truncated"):
            out.notes.append(f"Only the newest {status.get(f'{part}_read', 0):,} {what} were read.")
