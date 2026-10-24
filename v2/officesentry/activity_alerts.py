"""Activity alerts: risky changes made in a client's tenant, posted once each (the webhook, ``alerts.deliver``).

After each Admin activity run (``collectors/admin_activity.py``), the changes it holds that the client's previous
readable run didn't are new: they happened since, or Microsoft logged them late. Those that match a kind the
admin ticked under *Settings → Alerts* (``alerts.ACTIVITY_ALERTS``) go in one message per client per run:

  * ``admin_role``: someone given an admin role (``PRIVILEGED_ROLE_NAMES``), active or eligible; not the
    activation of a role someone is already eligible for, which is how PIM is meant to be used;
  * ``ca_off``: a Conditional Access policy deleted, turned off or set to report-only;
  * ``security_defaults_off``: security defaults turned off, or changed in a way the event doesn't say;
  * ``admin_mfa_removed``: a sign-in method removed from someone who holds an admin role, by an admin or by
    themselves.

Nothing is sent for a client's first readable run (that would be everything Microsoft still keeps), for a
change more than ``MAX_AGE_DAYS`` old when first read (a client that couldn't be collected for a while: the
Admin activity report lists them), for a failed attempt, or for a client that is paused or archived. Never
raises into the worker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import adminactivity, alerts, store
from .collectors.identity import PRIVILEGED_ROLE_NAMES
from .devices import parse_ts
from .formatting import when
from .state_history import READABLE, objects_of
from .text import plural

log = logging.getLogger(__name__)

COLLECTOR = "admin_activity"
MAX_AGE_DAYS = 7
LOOKBACK = 10  # earlier runs tried for one whose audit log was read
GLOBAL_ADMIN = "Global Administrator"
# Short names for each kind, beside each change in Teams and Slack.
LABELS = {
    "admin_role": "Admin role",
    "ca_off": "Conditional Access",
    "security_defaults_off": "Security defaults",
    "admin_mfa_removed": "Admin sign-in method",
}
_METHOD_REMOVED = ("admin deleted security info", "user deleted security info")


@dataclass
class Change:
    kind: str  # a key of alerts.ACTIVITY_ALERTS
    severity: str
    text: str  # "bob@contoso.com made Global Administrator"
    event: dict  # the change as adminactivity.normalise stored it


def _who(event: dict) -> str:
    return ", ".join(str(t) for t in event.get("targets") or [] if t) or "Someone"


def match(event: dict, admins: dict[str, list[str]]) -> Change | None:
    """The alert one stored change calls for, or None. ``admins`` is each admin's sign-in name (lower case) and
    the admin roles they hold, for a sign-in method removed."""
    if not isinstance(event, dict) or event.get("result") != "success":
        return None
    kind, activity = event.get("kind"), adminactivity._norm(str(event.get("activity") or ""))
    detail = str(event.get("detail") or "")
    if kind == "role_grant" and detail in PRIVILEGED_ROLE_NAMES:
        made = f"made eligible for {detail}" if "eligible" in activity else f"made {detail}"
        return Change("admin_role", "high" if detail == GLOBAL_ADMIN else "medium", f"{_who(event)} {made}", event)
    if kind == "ca" and (activity.startswith("delete conditional access policy") or event.get("ca_disabled")):
        if activity.startswith("delete"):
            what = "deleted"
        elif detail.endswith("Report-only"):
            what = "set to report-only"
        else:
            what = "turned off"
        name = _who(event) if event.get("targets") else "an unnamed policy"
        return Change("ca_off", "high", f"Conditional Access policy {what}: {name}", event)
    if kind == "auth_policy" and activity == adminactivity.SECURITY_DEFAULTS and event.get("defaults_off") is not False:
        text = "Security defaults turned off" if event.get("defaults_off") else "Security defaults changed"
        return Change("security_defaults_off", "high", text, event)
    if kind == "security_info" and activity in _METHOD_REMOVED:
        roles = {role for t in event.get("targets") or [] for role in admins.get(str(t).lower(), [])}
        held = sorted(roles, key=lambda r: (r != GLOBAL_ADMIN, r))
        if held:
            what = held[0] if len(held) == 1 else f"{held[0]} and {plural(len(held) - 1, 'other role')}"
            return Change("admin_mfa_removed", "medium", f"Sign-in method removed from {_who(event)} ({what})", event)
    return None


def _audit_read(conn, run_id: int) -> bool:
    window = (objects_of(conn, run_id, "audit_window") or [None])[0]
    return bool(window and window.get("available"))


def new_events(conn, run, now: datetime | None = None) -> list[dict] | None:
    """The changes ``run`` holds that the client's previous readable Admin activity run didn't, from the last
    ``MAX_AGE_DAYS``; None when there is nothing to compare with (the first time the audit log was read)."""
    if not _audit_read(conn, run["id"]):
        return None
    earlier = conn.execute(
        "SELECT id FROM runs WHERE tenant_id = ? AND collector = ? AND id < ? AND status IN ('succeeded', 'partial')"
        f" AND {READABLE} ORDER BY id DESC LIMIT ?",
        (run["tenant_id"], COLLECTOR, run["id"], LOOKBACK),
    ).fetchall()
    base = next((r["id"] for r in earlier if _audit_read(conn, r["id"])), None)
    if base is None:
        return None
    seen = {o["_id"] for o in objects_of(conn, base, "admin_event")}
    floor = (now or datetime.now(UTC)) - timedelta(days=MAX_AGE_DAYS)
    return [
        o
        for o in store.run_objects(conn, run["id"], "admin_event")
        if o["_id"] not in seen and (at := parse_ts(o.get("time"))) is not None and at >= floor
    ]


def admins(conn, tenant_id: int) -> dict[str, list[str]]:
    """Each admin's sign-in name (lower case) and the admin roles they hold, active or eligible, from the
    latest Admin roles run."""
    run = store.latest_run(conn, tenant_id, "admin_roles")
    out: dict[str, set[str]] = {}
    for a in store.run_objects(conn, run["id"], "role_assignment") if run else []:
        if a.get("privileged") and a.get("principalType") == "user" and a.get("userPrincipalName"):
            out.setdefault(str(a["userPrincipalName"]).lower(), set()).add(str(a.get("roleName") or ""))
    return {upn: sorted(r for r in roles if r) for upn, roles in out.items()}


def changes(conn, run, kinds, now: datetime | None = None) -> list[Change]:
    """The new changes in ``run`` of the alert ``kinds`` chosen, oldest first."""
    found = new_events(conn, run, now)
    if not found:
        return []
    held = admins(conn, run["tenant_id"]) if "admin_mfa_removed" in kinds else {}
    picked = [c for e in found if (c := match(e, held)) is not None and c.kind in kinds]
    return sorted(picked, key=lambda c: str(c.event.get("time") or ""))


def item(change: Change) -> alerts.Item:
    e = change.event
    by = f"by {e.get('actor') or 'someone'}, {when(e.get('time'))}"
    return alerts.Item(
        change.severity,
        LABELS[change.kind],
        change.text,
        by,
        {
            "type": change.kind,
            "activity": e.get("activity"),
            "time": e.get("time"),
            "actor": e.get("actor"),
            "ip": e.get("ip") or None,
            "targets": list(e.get("targets") or []),
            "detail": e.get("detail") or None,
        },
        label=LABELS[change.kind],
        key=f"change:{e['_id']}" if e.get("_id") else "",
    )


def message(base_url: str, tenant, found: list[Change]) -> alerts.Message:
    n = len(found)
    title = f"{tenant['name']}: " + (found[0].text if n == 1 else f"{n} changes to check")
    return alerts.Message(
        "activity.changes",
        title,
        "Read from the client's directory audit log. If you didn't expect "
        + ("it" if n == 1 else "one")
        + ", check who made it and why.",
        f"{alerts.tenant_link(base_url, tenant)}/data/admin-activity",
        dict(tenant),
        items=[item(c) for c in found],
        button="Open Admin activity",
        noun="change",
    )


def after_run(conn, alerting: alerts.Alerting | None, run_id: int, status: str) -> bool | None:
    """Post the new changes one Admin activity run found, when the admin chose any. Returns whether a message
    was sent, None if none was needed. Never raises."""
    try:
        if alerting is None or status not in ("succeeded", "partial"):
            return None
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run is None or run["collector"] != COLLECTOR:
            return None
        s = alerts.settings(conn)
        kinds = alerts.activity_kinds(s)
        if not kinds or not alerts.wanted(conn, s):
            return None
        tenant = alerts.alerted_tenant(conn, run["tenant_id"])
        if tenant is None:
            return None
        found = changes(conn, run, kinds)
        if not found:
            return None
        return alerts.deliver(conn, alerting, message(alerting.base_url, tenant, found), run_id, s)
    except Exception:
        log.exception("Preparing an activity alert failed")
        return False


def sample_message(base_url: str) -> alerts.Message:
    """What an activity alert looks like, for the settings page's test button. Names no real tenant."""
    at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    event = {
        "activity": "Add member to role",
        "time": at,
        "actor": "it.admin@contoso.example",
        "targets": ["new.starter@contoso.example"],
        "detail": GLOBAL_ADMIN,
    }
    found = [Change("admin_role", "high", f"new.starter@contoso.example made {GLOBAL_ADMIN}", event)]
    sample = message(base_url, {"id": 0, "name": "Contoso (example)", "tenant_ref": None}, found)
    sample.event, sample.link = "test", base_url.rstrip("/")
    return sample
