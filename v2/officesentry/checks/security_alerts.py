"""Checks over Microsoft Defender's alerts."""

from __future__ import annotations

from datetime import datetime

from .. import clouds
from .. import defender as d
from ..snapshot import TenantSnapshot
from ..text import plural
from .base import Finding, _unavailable, rule


def _detail(alert: dict) -> str:
    """Where an open alert came from, who has it, since when and what it names."""
    source = d.SOURCES.get(alert.get("source") or "", "Defender")
    owner = alert.get("assignedTo")
    if d.untouched(alert):
        who = "nobody has picked it up"
    elif alert.get("status") == "inProgress":
        who = f"in progress with {owner}" if owner else "in progress"
    else:
        who = f"assigned to {owner}"
    since = f", since {alert['created'][:10]}" if isinstance(alert.get("created"), str) else ""
    names = [*(alert.get("users") or []), *(alert.get("devices") or [])][:3]
    return f"{source}, {who}{since}" + (f"; names {', '.join(names)}" if names else "")


@rule(
    "defender_alerts",
    "No high or medium severity Defender alerts are left open",
    "Threats",
    "In the Microsoft Defender portal (Incidents & alerts), assign each open alert to someone, check what it names "
    "(for an account: recent sign-ins, inbox rules and MFA methods; for a device: what Defender found on it), then "
    "resolve it saying whether it was a real threat. Set up email notifications for new incidents (Settings > "
    "Microsoft Defender XDR > Email notifications) so they're seen the day they happen. The Security alerts report "
    "lists each alert.",
)
def defender_alerts(snap: TenantSnapshot, now: datetime):
    if why := clouds.missing(clouds.of(snap.tenant), "security_alerts"):
        return _unavailable(why)
    status = d.status(snap)
    if status is None:
        return _unavailable("Security alerts haven't been collected yet")
    state = (status.get("parts") or {}).get("alerts")
    if state == "waiting":
        return _unavailable(f"Waiting for the client to approve {d.ALERTS}")
    if state == "unavailable":
        return _unavailable("Microsoft Defender has no alerts for this tenant")
    if state != "read":
        return _unavailable("Defender alerts couldn't be read")
    findings, untouched = [], 0
    for a in d.ordered(d.alerts(snap), d.alert_open):
        severity = a.get("severity")
        if not d.alert_open(a) or severity not in ("high", "medium"):
            continue
        untouched += d.untouched(a)
        findings.append(Finding(severity, a.get("title") or "Untitled alert", _detail(a), a.get("_id")))
    if not findings:
        if status.get("alerts_truncated"):
            return _unavailable("Too many alerts to read them all, so this can't confirm none are open")
        return ("pass", "No high or medium severity alerts are open", [])
    return (
        "fail",
        f"{plural(len(findings), 'high or medium severity alert')} open in Defender"
        + (f", {untouched} not picked up" if untouched else ""),
        findings,
    )
