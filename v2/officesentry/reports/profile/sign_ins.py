"""Sign-ins and admin changes: recent activity, sign-in history and Entra ID Protection risk."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ... import events as events_store
from ... import identity_protection, signins
from ...licences import last_sign_in
from ...text import plural as _plural
from ..data_reports import Section
from ..identity_reports import P1
from .base import SIGNIN_HISTORY_DAYS, Part, Person, _day, _when, part


@part(90)
def activity_part(p: Person) -> Part:
    result = Part("activity", "Sign-ins and admin changes")
    u = p.user
    if u.get("signInActivityAvailable"):
        for key, label in (
            ("lastSignInDateTime", "Last interactive sign-in"),
            ("lastNonInteractiveSignInDateTime", "Last background sign-in"),
            ("lastSuccessfulSignInDateTime", "Last successful sign-in"),
        ):
            result.facts.append((label, _when(u.get(key), p.now) or ("None recorded" if last_sign_in(u) else "Never")))
        last = last_sign_in(u)
        result.summary = _when(last, p.now).split(" (")[-1].rstrip(")").capitalize() if last else "Never"
    else:
        result.facts.append(("Last sign-in", f"Not checked: sign-in dates need {P1}"))

    status = (p.snap.objects("legacy_signins", "legacy_signin_status") or [None])[0]
    legacy = [
        s
        for s in p.snap.objects("legacy_signins", "legacy_signin") or []
        if s.get("userId") == p.id or p.named(s.get("user"))
    ]
    if legacy:
        table = Section(
            "Legacy sign-ins in the last 30 days", ["Client", "Succeeded", "Failed", "Last attempt", "Countries"]
        )
        for s in sorted(legacy, key=lambda s: s.get("lastAttempt") or "", reverse=True):
            table.add(
                [
                    s.get("client"),
                    s.get("succeeded") or 0,
                    s.get("failed") or 0,
                    _when(s.get("lastAttempt"), p.now),
                    s.get("countries") or [],
                ],
                "medium" if s.get("succeeded") else "low",
            )
        result.tables.append(table)
        ok = sum(s.get("succeeded") or 0 for s in legacy)
        if ok:
            clients = ", ".join(sorted({s.get("client") or "?" for s in legacy if s.get("succeeded")}))
            p.flag(
                "medium",
                f"Signed in {_plural(ok, 'time')} with legacy authentication ({clients}) in the last "
                "30 days. These sign-ins skip MFA.",
                "activity",
            )
    elif status and status.get("available"):
        result.facts.append(("Legacy sign-ins (30 days)", "None"))

    window = (p.snap.objects("admin_activity", "audit_window") or [None])[0]
    history = p.admin_history()
    period = history[1] if history else "the last 30 days"
    events = []
    for e in history[0] if history else p.snap.objects("admin_activity", "admin_event") or []:
        by = p.named(e.get("actor"))
        to = any(p.named(t) for t in e.get("targets") or [])
        if by or to:
            events.append((e, by))
    if events:
        table = Section(f"Admin changes in {period}", ["When (UTC)", "Change", "By", "Target", "Detail"], compare=False)
        for e, by in sorted(events, key=lambda x: x[0].get("time") or "", reverse=True):
            table.add(
                [
                    (e.get("time") or "")[:16].replace("T", " "),
                    e.get("activity"),
                    e.get("actor"),
                    ", ".join(e.get("targets") or []),
                    e.get("detail"),
                ],
                "medium" if e.get("kind") in ("role_grant",) and not by else None,
            )
        result.tables.append(table)
    elif history or (window and window.get("available")):
        result.facts.append((f"Admin changes ({period.removeprefix('the ')})", "None by or to this account"))
    kept = events_store.coverage(p.snap.conn, p.snap.tenant["id"], "signins").detailed_from
    result.note = (
        f"Office Sentry keeps this tenant's sign-in log from {_day(datetime.fromtimestamp(kept, UTC))}: "
        "each day is under Sign-in history, and the last week in detail under Recent sign-ins. Legacy "
        "sign-ins and the last sign-in dates show here."
        if kept is not None
        else "Office Sentry doesn't keep the full sign-in log; the last week is summarised under Recent "
        "sign-ins, and legacy sign-ins and the last sign-in dates show here."
    )
    return result


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _utc(value) -> str:
    return value[:16].replace("T", " ") if isinstance(value, str) else ""


@part(85)
def sign_ins_part(p: Person) -> Part | None:
    """The last week of the sign-in log for this account, from the summaries the sign_ins collector keeps."""
    status = signins.status(p.snap)
    if status is None:
        return None
    days = status.get("days") if isinstance(status.get("days"), int) else signins.WINDOW_DAYS
    result = Part("signins", "Recent sign-ins")
    if not status.get("available"):
        result.unavailable = (
            "Not checked: Office Sentry can't read this tenant's sign-in log"
            if status.get("reason") == "permission"
            else f"Not checked: the sign-in log needs {P1}"
        )
        return result
    mine = lambda r: r.get("userId") == p.id or p.named(r.get("user"))  # noqa: E731
    row = signins.for_account(p.snap, p.id, p.keys) or {}
    risky = [r for r in p.snap.objects("sign_ins", "risky_sign_in") or [] if mine(r)]
    if not row and not risky:
        result.summary, result.empty = "None", f"No interactive sign-ins in the last {days} days."
        return result

    def n(key: str) -> int:
        return _int(row.get(key))

    unusual = signins.unusual_countries(p.snap)
    locations = [r for r in p.snap.objects("sign_ins", "sign_in_location") or [] if mine(r)]
    countries = [c for c in row.get("countries") or [] if isinstance(c, str)]
    ok, failed, single, mfa = n("succeeded"), n("failed"), n("singleFactor"), n("mfa")
    split = ", ".join(
        x for x in (f"{mfa} with MFA" if mfa else "", f"{single} with only a password" if single else "") if x
    )
    result.facts.append(("Successful", f"{ok} ({split})" if split else str(ok)))
    how = [
        f"{n('credentialFailures')} wrong password" if n("credentialFailures") else "",
        _plural(n("lockouts"), "lockout") if n("lockouts") else "",
        f"from {_plural(n('failureIpCount'), 'address', 'addresses')}" if n("failureIpCount") else "",
    ]
    how = [h for h in how if h]
    result.facts.append(("Failed", (f"{failed} ({', '.join(how)})" if how else str(failed)) if failed else "None"))
    if n("interrupted"):
        result.facts.append(("MFA and other prompts", n("interrupted")))
    if row.get("lastFailure"):
        result.facts.append(
            (
                "Last failure",
                f"{_when(row.get('lastFailure'), p.now)}: "
                f"{signins.meaning(row.get('lastFailureCode'), row.get('lastFailureReason'))}",
            )
        )
    if countries:
        result.facts.append(
            ("Countries", ", ".join(c + (" (unusual for this tenant)" if c in unusual else "") for c in countries))
        )
    for key, label in (("apps", "Apps"), ("operatingSystems", "Devices"), ("failureIps", "Addresses that failed")):
        values = [v for v in row.get(key) or [] if isinstance(v, str)]
        if values:
            result.facts.append((label, ", ".join(values)))

    odd = []
    places = Section(
        "Where this account signed in",
        ["Country", "Successful", "Failed", "Cities", "IP addresses", "Last success (UTC)"],
    )
    for loc in sorted(locations, key=lambda r: -_int(r.get("succeeded"))):
        unusual_here = loc.get("country") in unusual and _int(loc.get("succeeded")) > 0
        if unusual_here:
            odd.append(loc["country"])
        places.add(
            [
                signins.country(loc.get("country")),
                _int(loc.get("succeeded")),
                _int(loc.get("failed")),
                loc.get("cities") or [],
                loc.get("ips") or [],
                _utc(loc.get("lastSuccess")),
            ],
            "medium" if unusual_here else None,
        )
    if places.rows:
        result.tables.append(places)
    codes = row.get("failureCodes") if isinstance(row.get("failureCodes"), dict) else {}
    if codes:
        reasons = Section("Why sign-ins failed", ["Error code", "What it means", "Sign-ins"])
        for code, count in sorted(codes.items(), key=lambda c: -_int(c[1])):
            reasons.add([str(code), signins.meaning(code), _int(count)])
        result.tables.append(reasons)
    worst = None
    if risky:
        table = Section("Risky sign-ins", ["When (UTC)", "Risk level", "Risk state", "Result", "Country", "IP address"])
        for r in sorted(risky, key=lambda r: r.get("time") or "", reverse=True):
            severity = signins.risk_severity(r.get("level"), r.get("state")) if r.get("succeeded") else None
            if severity and (worst is None or (severity == "high" and worst[0] != "high")):
                worst = (severity, r)
            table.add(
                [
                    _utc(r.get("time")),
                    (r.get("level") or "Unknown").capitalize(),
                    signins.RISK_STATES.get(r.get("state"), r.get("state")),
                    "Signed in" if r.get("succeeded") else signins.meaning(r.get("errorCode")),
                    signins.country(r.get("country")),
                    r.get("ip"),
                ],
                severity or "low",
            )
        result.tables.append(table)

    if worst:
        severity, r = worst
        p.flag(
            severity,
            f"Signed in on {_day(r.get('time'))} from {signins.country(r.get('country'))} although "
            f"Entra ID rated the sign-in {r.get('level') or 'risky'} risk. Check with the person; if "
            "they don't recognise it, reset the password and sign them out everywhere.",
            "signins",
        )
    if failed >= signins.REPEATED_FAILURES:
        p.flag(
            "high" if n("lockouts") else "medium",
            f"{_plural(failed, 'failed sign-in')} in the last {days} days"
            + (f", {n('credentialFailures')} with a wrong password" if n("credentialFailures") else "")
            + (", and locked out" if n("lockouts") else "")
            + ". If the person didn't make them, someone is guessing the password: make sure MFA is enforced.",
            "signins",
        )
    if odd:
        p.flag(
            "medium",
            f"Signed in from {', '.join(odd)}, which this tenant rarely signs in from. Check the person was there.",
            "signins",
        )
    if single and (p.admin or not mfa):
        p.flag(
            "medium" if p.admin else "low",
            f"Signed in {_plural(single, 'time')} with only a password in the last {days} days"
            + ("" if mfa else ", never with MFA")
            + ".",
            "signins",
        )
    result.summary = f"{_plural(n('signIns'), 'sign-in')}, {failed} failed"
    result.tone = "bad" if worst else ("warn" if failed >= signins.REPEATED_FAILURES or odd else None)
    result.note = (
        f"Interactive sign-ins in the last {days} days from the Entra ID sign-in log. MFA and other "
        "prompts aren't counted as failures."
    )
    return result


@part(87)
def sign_in_history_part(p: Person) -> Part | None:
    """Month by month from the activity history's daily sign-in summaries, back as far as they're kept."""
    first = events_store.coverage(p.snap.conn, p.snap.tenant["id"], "signins").detailed_from
    if first is None:
        return None
    kept = datetime.fromtimestamp(first, UTC)
    start = max(kept, p.end - timedelta(days=SIGNIN_HISTORY_DAYS))
    rows = events_store.daily_for(
        p.snap.conn, p.snap.tenant["id"], {p.id, *p.keys}, f"{start:%Y-%m-%d}", f"{p.end:%Y-%m-%d}"
    )
    result = Part("signin_history", "Sign-in history")
    result.note = (
        f"Every interactive sign-in since {_day(kept)}, from the activity history's daily "
        "summaries, kept as long as Data retention says (25 months by default)."
    )
    if not rows:
        result.summary, result.empty = "None", f"No interactive sign-ins since {_day(start)}."
        return result
    recent = f"{p.end - timedelta(days=30):%Y-%m-%d}"
    days_30 = {r.day for r in rows if r.day > recent}
    result.summary = f"Signed in on {_plural(len(days_30), 'day')} of the last 30"
    result.facts.append(("History from", f"{_day(kept)}"))
    result.facts.append(("Last sign-in", _when(max(r.last for r in rows), p.now)))
    countries = sorted({r.country for r in rows if r.country})
    if countries:
        result.facts.append(("Countries", ", ".join(countries)))
    months: dict[str, dict] = {}
    for r in rows:
        m = months.setdefault(
            r.day[:7], {"days": set(), "ok": 0, "failed": 0, "mfa": 0, "single": 0, "countries": set()}
        )
        m["days"].add(r.day)
        m["ok"] += r.succeeded
        m["failed"] += r.failed
        m["mfa"] += r.mfa
        m["single"] += r.single_factor
        if r.country:
            m["countries"].add(r.country)
    table = Section(
        "Sign-ins by month",
        ["Month", "Days", "Successful", "Failed", "With MFA", "Password only", "Countries"],
        compare=False,
    )
    for month in sorted(months, reverse=True):
        m = months[month]
        table.add(
            [
                f"{datetime.strptime(month, '%Y-%m'):%b %Y}",
                len(m["days"]),
                m["ok"],
                m["failed"],
                m["mfa"],
                m["single"],
                sorted(m["countries"]),
            ],
            "low" if m["single"] and (p.admin or not m["mfa"]) else None,
        )
    result.tables.append(table)
    return result


@part(86)
def risk_part(p: Person) -> Part | None:
    """Entra ID Protection's user risk for this account; shown only when it's at risk or was resolved recently."""
    status = identity_protection.status(p.snap)
    if not status or not status.get("available"):
        return None
    row = next((r for r in identity_protection.rows(p.snap) if r.get("userId") == p.id or p.named(r.get("user"))), None)
    if row is None:
        return None
    result = Part("risk", "Identity Protection risk")
    level, state = identity_protection.level(row), identity_protection.state(row)
    result.facts += [("Risk level", level), ("Risk state", state)]
    if identity_protection.detail(row):
        result.facts.append(("Detail", identity_protection.detail(row)))
    if row.get("updated"):
        result.facts.append(("Last updated", _when(row.get("updated"), p.now)))
    if identity_protection.open_risk(row):
        severity = identity_protection.severity(row)
        result.summary, result.tone = f"{level} risk, {state.lower()}", "bad" if severity == "high" else "warn"
        p.flag(
            severity,
            f"Entra ID Protection rates this account {level.lower()} risk ({state.lower()}). Check "
            "recent sign-ins with the person; if they don't recognise them, reset the password and sign "
            "them out everywhere, then confirm or dismiss the risk.",
            "risk",
        )
    else:
        result.summary, result.tone = state or "Resolved", "ok"
    return result
