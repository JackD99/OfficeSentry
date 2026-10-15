"""Timeline: what happened to the account, for a suspected compromise."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlsplit

from ... import events as events_store
from ... import signins, store
from ...auditlog import addon, operations
from ...checks.apps import SENSITIVE_DELEGATED
from ...formatting import day
from ...formatting import when as at_time
from ...mailflow import inbox_rule_risk, is_external, mailbox_forwarding, tenant_domains
from ...text import plural as _plural
from ..data_reports import Section
from ..identity_reports import _methods
from .base import Part, Person, part
from .history import _object_history, _role_history
from .sign_ins import _int

TIMELINE_DAYS = 90
TIMELINE_LIMIT = 50
AREAS = {"files": "Files", "sharing": "Sharing", "audit": "Audit log"}  # the add-on's groups; the rest are Mailbox


def _sightings(
    conn, tid: int, collector: str, kind: str, key: str, value: str
) -> tuple[dict[str, list[tuple[str, dict, int]]], str | None, int | None]:
    """Each object whose ``key`` is ``value``: its copies across retained snapshots ({object id:
    [(finished_at, data, run ID)]}, oldest first), when the oldest retained snapshot finished and the latest
    run's ID."""
    oldest, latest = _retained(conn, tid, collector)
    seen: dict[str, list[tuple[str, dict, int]]] = {}
    for object_id, finished_at, run_id, data in store.matching_objects(conn, tid, collector, kind, key, value):
        seen.setdefault(object_id, []).append((finished_at, data, run_id))
    return seen, oldest, latest


def _retained(conn, tid: int, collector: str) -> tuple[str | None, int | None]:
    """When the collector's oldest snapshot that pruning has left finished, and its latest run's ID."""
    row = conn.execute(
        "SELECT MIN(finished_at) AS oldest, MAX(id) AS latest FROM runs r WHERE tenant_id = ? AND collector = ?"
        " AND status IN ('succeeded', 'partial')"
        " AND (object_count = 0 OR EXISTS (SELECT 1 FROM objects o WHERE o.run_id = r.id))",
        (tid, collector),
    ).fetchone()
    return row["oldest"], row["latest"]


@dataclass
class _Happened:
    at: str  # ISO date or time, for sorting
    when: str  # as shown
    area: str
    text: str
    tone: str | None = None


def _first_seen(at: str, oldest: str | None) -> str:
    return f"On or before {day(at)}" if oldest and at <= oldest else day(at)


def timeline(p: Person) -> list[_Happened]:
    """Dated events for the account, newest first: admin changes, sign-in highlights and when inbox rules,
    forwarding, app consents, MFA methods and admin roles were first collected."""
    conn, tid = p.snap.conn, p.snap.tenant["id"]
    out: list[_Happened] = []

    def timed(value, area, text, tone=None):
        if isinstance(value, str) and value:
            out.append(_Happened(value, at_time(value), area, text, tone))

    def first(at, oldest, area, text, tone=None):
        out.append(_Happened(at, _first_seen(at, oldest), area, text, tone))

    history = p.admin_history()
    for e in history[0] if history else p.snap.objects("admin_activity", "admin_event") or []:
        by = p.named(e.get("actor"))
        if not by and not any(p.named(t) for t in e.get("targets") or []):
            continue
        detail = f": {e['detail']}" if e.get("detail") else ""
        text = (
            f"{e.get('activity')}{detail}"
            + (f" (by {e.get('actor')})" if not by and e.get("actor") else "")
            + (f" (to {', '.join(e.get('targets') or [])})" if by and e.get("targets") else "")
        )
        timed(
            e.get("time"),
            "Admin change" if not by else "Done by this account",
            text,
            "warn" if e.get("kind") in ("role_grant", "consent") or e.get("credential_added") else None,
        )

    mine = lambda r: r.get("userId") == p.id or p.named(r.get("user"))  # noqa: E731
    row = signins.for_account(p.snap, p.id, p.keys) or {}
    if row.get("lastFailure"):
        timed(
            row["lastFailure"],
            "Sign-in",
            f"Last of {_plural(_int(row.get('failed')), 'failed sign-in')}: "
            f"{signins.meaning(row.get('lastFailureCode'), row.get('lastFailureReason'))}",
            "warn" if _int(row.get("failed")) >= signins.REPEATED_FAILURES else None,
        )
    for r in p.snap.objects("sign_ins", "risky_sign_in") or []:
        if mine(r):
            timed(
                r.get("time"),
                "Sign-in",
                f"{(r.get('level') or 'Risky').capitalize()} risk sign-in from "
                f"{signins.country(r.get('country'))} ({r.get('ip') or 'unknown address'})"
                + ("" if r.get("succeeded") else ", blocked"),
                "bad" if r.get("succeeded") else "warn",
            )
    unusual = signins.unusual_countries(p.snap)
    for loc in p.snap.objects("sign_ins", "sign_in_location") or []:
        if mine(loc) and loc.get("country") in unusual and loc.get("lastSuccess"):
            timed(
                loc["lastSuccess"],
                "Sign-in",
                f"Signed in from {loc['country']}, unusual for this tenant"
                + (f" ({', '.join(loc.get('ips') or [])})" if loc.get("ips") else ""),
                "warn",
            )
    for s in p.snap.objects("legacy_signins", "legacy_signin") or []:
        if mine(s):
            timed(
                s.get("lastAttempt"),
                "Sign-in",
                f"Legacy sign-in with {s.get('client') or 'an old protocol'}: "
                f"{s.get('succeeded') or 0} succeeded, {s.get('failed') or 0} failed",
                "warn" if s.get("succeeded") else None,
            )
    kept = events_store.coverage(conn, tid, "signins").detailed_from
    if kept is not None:
        # The activity history has every day: the first day each country shows up, exactly.
        start = datetime.fromtimestamp(kept, UTC)
        firsts: dict[str, str] = {}
        for r in events_store.daily_for(conn, tid, {p.id, *p.keys}, f"{start:%Y-%m-%d}", f"{p.end:%Y-%m-%d}"):
            if r.country and r.succeeded and r.country not in firsts:
                firsts[r.country] = r.first
        for country, at in firsts.items():
            if at[:10] > f"{start + timedelta(days=1):%Y-%m-%d}":  # not just where the history starts
                timed(at, "Sign-in", f"First sign-in from {signins.country(country)}", "warn")
    else:
        places, oldest, _ = _sightings(conn, tid, "sign_ins", "sign_in_location", "userId", p.id)
        for copies in places.values():
            at = copies[0][0]
            if oldest and at > oldest:  # a country this account hadn't signed in from before
                first(
                    at,
                    oldest,
                    "Sign-in",
                    f"First sign-in from {signins.country(copies[-1][1].get('country'))} seen",
                    "warn",
                )

    domains = tenant_domains(p.snap) or set()
    rules, oldest, latest = _sightings(conn, tid, "inbox_rules", "inbox_rule", "mailboxId", p.id)
    for copies in rules.values():
        data = copies[-1][1]
        risk = inbox_rule_risk(data, domains)
        first(
            copies[0][0],
            oldest,
            "Mailbox",
            f'Inbox rule "{data.get("displayName")}" first seen'
            + ("" if copies[-1][2] == latest else " (since removed)")
            + (f": {'; '.join(risk.reasons)}" if risk.reasons else ""),
            {"high": "bad", "medium": "warn"}.get(risk.severity),
        )

    boxes = _object_history(conn, tid, "mailboxes", "mailbox", p.id)
    box_oldest, _ = _retained(conn, tid, "mailboxes")
    targets: dict[str, str] = {}
    for at, data in boxes:
        target = mailbox_forwarding(data)
        if target:
            targets.setdefault(target, at)
    for target, at in targets.items():
        first(
            at,
            box_oldest,
            "Mailbox",
            f"Forwarding to {target} first seen"
            + (" (outside the organisation)" if is_external(target, domains) else ""),
            "bad" if is_external(target, domains) else "warn",
        )

    grants, oldest, latest = _sightings(conn, tid, "app_consents", "delegated_grant", "principalId", p.id)
    sps = {s["_id"]: s for s in p.snap.objects("app_consents", "service_principal") or []}
    for copies in grants.values():
        data = copies[-1][1]
        if data.get("consentType") != "Principal":
            continue
        app = (sps.get(data.get("clientId")) or {}).get("displayName") or "An app"
        scopes = sorted(s for s in data.get("scopes") or [] if s)
        first(
            copies[0][0],
            oldest,
            "Apps",
            f"Allowed {app}"
            + (f" ({', '.join(scopes)})" if scopes else "")
            + ("" if copies[-1][2] == latest else " (since removed)"),
            "warn" if set(scopes) & SENSITIVE_DELEGATED else None,
        )

    mfa = _object_history(conn, tid, "mfa_registration", "mfa_registration", p.id)
    methods: dict[str, str] = {}
    for at, data in mfa:
        for m in _methods(data):
            methods.setdefault(m, at)
    oldest, _ = _retained(conn, tid, "mfa_registration")
    for m, at in methods.items():
        first(at, oldest, "MFA", f"{m} first seen as an MFA method")

    held: dict[str, str] = {}
    for at, names in _role_history(conn, tid, p.id):
        for name in names:
            held.setdefault(name, at)
    oldest, _ = _retained(conn, tid, "admin_roles")
    for name, at in held.items():
        first(at, oldest, "Admin roles", f"{name} role first seen", "warn")

    if addon.enabled(conn) and addon.row_for(conn, tid) is not None:  # off for the install hides it here too
        out.extend(_audit_log(p))

    cutoff = (p.now - timedelta(days=TIMELINE_DAYS)).strftime("%Y-%m-%d")
    out = [e for e in out if e.at[:10] >= cutoff]
    out.sort(key=lambda e: e.at, reverse=True)
    return out[:TIMELINE_LIMIT]


def _item(value: str) -> str:
    return unquote(urlsplit(value).path) or value if value.lower().startswith("https://") else value


def _audit_log(p: Person) -> list[_Happened]:
    """The audit log add-on's records (``auditlog/``) about the account: what it did to files, sharing and
    mailboxes, and what someone else did to its mailbox. Its own file deletes and downloads are counted per day
    and kind, so a busy day is one line."""
    out: list[_Happened] = []
    files: Counter = Counter()
    last: dict[tuple[str, str], str] = {}
    start = p.end - timedelta(days=TIMELINE_DAYS)
    for a in events_store.activity_between(p.snap.conn, p.snap.tenant["id"], start, p.end, people=p.keys):
        at = f"{datetime.fromtimestamp(a.ts, UTC):%Y-%m-%dT%H:%M:%SZ}"
        group, what = operations.group(a.operation), operations.label(a.operation)
        if group == "files" and p.named(a.actor) and not a.attention:
            files[(at[:10], what)] += 1
            last.setdefault((at[:10], what), _item(a.object))
            continue
        if p.named(a.actor):
            about = a.owner if a.owner and not p.named(a.owner) else _item(a.object)
            text = f"{what}: {about}" if about else what
        else:  # done to the account's mailbox (or shared with it) by someone else
            who = a.actor or "someone"
            text = what.replace("by someone else", f"by {who}").replace("someone else", "this account")
            text = (text if who in text else f"{text} by {who}") + (f": {_item(a.object)}" if a.object else "")
        text += f" ({a.summary})" if a.summary else ""
        out.append(_Happened(at, at_time(at), AREAS.get(group, "Mailbox"), text, "warn" if a.attention else None))
    for (on, what), n in files.items():
        out.append(_Happened(on, day(on), "Files", f"{what}: {last[(on, what)]}" if n == 1 else f"{what} {n} times"))
    return out


@part(95)
def timeline_part(p: Person) -> Part:
    result = Part("timeline", "Timeline")
    events = timeline(p)
    if not events:
        result.summary, result.empty = "Nothing recorded", f"Nothing dated in the last {TIMELINE_DAYS} days."
    else:
        table = Section("Timeline", ["When", "Area", "What happened"], compare=False)
        for e in events:
            table.add([e.when, e.area, e.text], {"bad": "high", "warn": "medium"}.get(e.tone))
        result.tables = [table]
        result.summary = _plural(len(events), "event")
        result.tone = "bad" if any(e.tone == "bad" for e in events) else None
    timed = (
        "Admin changes, sign-ins and audit log records"
        if addon.enabled(p.snap.conn) and addon.row_for(p.snap.conn, p.snap.tenant["id"])
        else ("Admin changes and sign-ins")
    )
    result.note = (
        f"Newest first, the last {TIMELINE_DAYS} days (up to {TIMELINE_LIMIT} events). {timed} "
        "keep their own time; rules, forwarding, app consents, MFA methods and roles are dated "
        "when Office Sentry first collected them, so they were set up on or before that day."
    )
    return result
