"""Sign-in activity: the last week of interactive sign-ins, summarised.

Reads the Entra ID sign-in log (beta, for ``authenticationRequirement``: whether
a sign-in needed MFA or only a password), which needs Entra ID P1, into the
activity history (``events.py``): each run reads only what is new since the
last one, from a stored cursor, and the first run reads back the 30 days
Microsoft keeps. The week is then summarised from the stored sign-ins: one row
per account, per account and country, per country, per app, per failure reason
and per address that failed, plus the risky sign-ins themselves when the tenant
has Entra ID P2 (Identity Protection). Without P2 Microsoft hides sign-in risk.

Microsoft's sign-in log can time out on busy tenants. A range that fails with a
timeout or server error (after the Graph client's retries) is read again a day
at a time from the oldest sign-in already read. Each query gets one retry on a
timeout (the smaller ones are the fallback), and the fallback stops after
``FALLBACK_SECONDS``: when the log is down for the whole tenant, every window
times out, and without a limit the run would hold a worker thread for hours.
Whatever wasn't read stays in the cursor's gaps, and the next run reads it
first, so a timeout, a crash or the per-run page budget loses nothing.

The legacy sign-in collector (``mail_usage.py``) reads the same stored log for
its 30 days once the history covers them.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import UTC, datetime, timedelta

from .. import events, signins
from ..graph import LicenseRequired, PermissionMissing
from ..text import plural
from .base import Collector, RunContext, register

PAGE_SIZE = 1000
SIGNIN_PAGES = 200  # pages read per run (200,000 sign-ins), newest first; the next run carries on from there
LOG_DAYS = 30  # how long Microsoft keeps the sign-in log; the first run reads back this far
FAILURE_IP_LIMIT = 200
RISKY_LIMIT = 500
QUERY_RETRIES = 1  # retries per sign-in log request on a timeout or server error (throttling gets the usual)
FALLBACK_SECONDS = 600  # how long to keep trying smaller queries once the big one has failed
clock = time.monotonic


def deadline(seconds: float = FALLBACK_SECONDS):
    """A function that says whether ``seconds`` have passed since this was called."""
    end = clock() + seconds
    return lambda: clock() >= end


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _top(counter: Counter, n: int | None = None) -> list[str]:
    return [k for k, _ in counter.most_common(n)]


class Summary:
    """Accumulates sign-ins page by page into the stored summaries."""

    def __init__(self, risk_visible: bool):
        self.total = 0
        self.risk_visible = risk_visible
        self.risk_seen = False
        self.accounts: dict[str, dict] = {}
        self.locations: dict[tuple[str, str], dict] = {}
        self.countries: dict[str, dict] = {}
        self.apps: dict[str, dict] = {}
        self.failures: dict[int, dict] = {}
        self.ips: dict[str, dict] = {}
        self.success_ips: Counter = Counter()
        self.risky: list[tuple[str, dict]] = []

    def add(self, s: dict) -> None:
        if not isinstance(s, dict) or s.get("isInteractive") is False:
            return
        self.total += 1
        status = s.get("status") if isinstance(s.get("status"), dict) else {}
        error = signins.code(status.get("errorCode"))
        result = signins.outcome(error)
        ok = result == "success"
        when = _text(s.get("createdDateTime"))
        user = _text(s.get("userPrincipalName")) or _text(s.get("userId")) or "Unknown"
        ukey = user.lower()
        location = s.get("location") if isinstance(s.get("location"), dict) else {}
        country = _text(location.get("countryOrRegion"))
        city = _text(location.get("city"))
        app = _text(s.get("appDisplayName")) or "Unknown app"
        ip = _text(s.get("ipAddress"))
        device = s.get("deviceDetail") if isinstance(s.get("deviceDetail"), dict) else {}
        mfa = self._mfa(s) if ok else None
        reason = _text(status.get("failureReason"))

        a = self.accounts.get(ukey)
        if a is None:
            a = self.accounts[ukey] = {
                "user": user, "userId": s.get("userId"), "displayName": s.get("userDisplayName"),
                "signIns": 0, "succeeded": 0, "failed": 0, "interrupted": 0, "mfa": 0, "singleFactor": 0,
                "credentialFailures": 0, "lockouts": 0, "risky": 0, "lastSignIn": "", "lastSuccess": None,
                "lastFailure": None, "lastFailureCode": None, "lastFailureReason": None,
                "_countries": Counter(), "_apps": Counter(), "_os": Counter(), "_failureIps": Counter(),
                "_codes": Counter()}
        a["signIns"] += 1
        a["lastSignIn"] = max(a["lastSignIn"], when)
        a["_countries"][country or "Unknown"] += 1
        a["_apps"][app] += 1
        if _text(device.get("operatingSystem")):
            a["_os"][device["operatingSystem"]] += 1
        if ok:
            a["succeeded"] += 1
            a["lastSuccess"] = max(a["lastSuccess"] or "", when)
            if mfa is True:
                a["mfa"] += 1
            elif mfa is False:
                a["singleFactor"] += 1
            if ip:
                self.success_ips[ip] += 1
        elif result == "interrupted":
            a["interrupted"] += 1
        else:
            a["failed"] += 1
            a["_codes"][str(error)] += 1
            a["credentialFailures"] += error in signins.CREDENTIAL_CODES
            a["lockouts"] += error == signins.LOCKOUT
            if ip:
                a["_failureIps"][ip] += 1
            if when >= (a["lastFailure"] or ""):
                a["lastFailure"], a["lastFailureCode"], a["lastFailureReason"] = when, error, reason or None

        loc = self.locations.setdefault((ukey, country), {
            "user": user, "userId": s.get("userId"), "country": country or None, "succeeded": 0, "failed": 0,
            "lastSuccess": None, "lastAttempt": "", "_ips": Counter(), "_apps": Counter(), "_cities": Counter()})
        loc["lastAttempt"] = max(loc["lastAttempt"], when)
        if ok:
            loc["succeeded"] += 1
            loc["lastSuccess"] = max(loc["lastSuccess"] or "", when)
        elif result == "failure":
            loc["failed"] += 1
        if ip:
            loc["_ips"][ip] += 1
        loc["_apps"][app] += 1
        if city:
            loc["_cities"][city] += 1

        self._tally(self.countries, country or "", {"country": country or None}, result, ukey, when)
        entry = self._tally(self.apps, app, {"app": app, "singleFactor": 0}, result, ukey, when)
        if mfa is False:
            entry["singleFactor"] += 1

        if not ok:
            f = self.failures.setdefault(error, {"code": error, "reason": None, "signIns": 0, "lastSeen": "",
                                                 "_accounts": set()})
            f["signIns"] += 1
            f["_accounts"].add(ukey)
            if when >= f["lastSeen"]:
                f["lastSeen"] = when
                f["reason"] = reason or f["reason"]
        if result == "failure" and ip:
            i = self.ips.setdefault(ip, {"ip": ip, "failures": 0, "credentialFailures": 0, "lastSeen": "",
                                         "_accounts": Counter(), "_countries": Counter()})
            i["failures"] += 1
            i["credentialFailures"] += error in signins.CREDENTIAL_CODES
            i["lastSeen"] = max(i["lastSeen"], when)
            i["_accounts"][user] += 1
            if country:
                i["_countries"][country] += 1

        self._risk(s, user, ok, error, when, country, city, ip, app)

    @staticmethod
    def _mfa(s: dict) -> bool | None:
        """True when a successful sign-in used MFA, False when a password (or other single factor) was enough."""
        return signins.mfa_used(s)

    @staticmethod
    def _tally(table: dict, key: str, start: dict, result: str, ukey: str, when: str) -> dict:
        row = table.get(key)
        if row is None:
            row = table[key] = {**start, "signIns": 0, "succeeded": 0, "failed": 0, "interrupted": 0,
                                "lastSeen": "", "_accounts": set()}
        row["signIns"] += 1
        row[{"success": "succeeded", "failure": "failed"}.get(result, "interrupted")] += 1
        row["_accounts"].add(ukey)
        row["lastSeen"] = max(row["lastSeen"], when)
        return row

    def _risk(self, s: dict, user: str, ok: bool, error: int, when: str, country: str, city: str, ip: str,
              app: str) -> None:
        level, state = s.get("riskLevelDuringSignIn"), s.get("riskState")
        if level not in (None, "hidden"):
            self.risk_seen = True
        if not self.risk_visible:
            return
        if level not in signins.RISK_LEVELS and state not in signins.OPEN_RISK:
            return
        account = self.accounts[user.lower()]
        account["risky"] += 1
        if len(self.risky) >= RISKY_LIMIT:
            return
        types = s.get("riskEventTypes_v2") or s.get("riskEventTypes") or []
        self.risky.append((_text(s.get("id")) or f"{user}|{when}|{len(self.risky)}", {
            "time": when, "user": user, "userId": s.get("userId"), "succeeded": ok, "errorCode": error,
            "level": level if isinstance(level, str) else None, "state": state if isinstance(state, str) else None,
            "detail": s.get("riskDetail") if isinstance(s.get("riskDetail"), str) else None,
            "eventTypes": [t for t in types if isinstance(t, str)] if isinstance(types, list) else [],
            "country": country or None, "city": city or None, "ip": ip or None, "app": app}))

    # -- Stored rows ----------------------------------------------------------------------------

    def account_rows(self) -> list[tuple[str, dict]]:
        rows = []
        for key, a in self.accounts.items():
            out = {k: v for k, v in a.items() if not k.startswith("_")}
            out["countries"] = _top(a["_countries"])
            out["apps"] = _top(a["_apps"], 10)
            out["operatingSystems"] = _top(a["_os"], 5)
            out["failureIps"] = _top(a["_failureIps"], 5)
            out["failureIpCount"] = len(a["_failureIps"])
            out["failureCodes"] = dict(a["_codes"].most_common(10))
            rows.append((str(a.get("userId") or key), out))
        return rows

    def location_rows(self) -> list[tuple[str, dict]]:
        rows = []
        for (ukey, country), loc in self.locations.items():
            out = {k: v for k, v in loc.items() if not k.startswith("_")}
            out["ips"] = _top(loc["_ips"], 5)
            out["apps"] = _top(loc["_apps"], 5)
            out["cities"] = _top(loc["_cities"], 5)
            rows.append((f"{ukey}|{country}", out))
        return rows

    @staticmethod
    def counted(table: dict) -> list[tuple[str, dict]]:
        rows = []
        for key, row in table.items():
            out = {k: v for k, v in row.items() if not k.startswith("_")}
            out["accounts"] = len(row["_accounts"])
            rows.append((str(key) or "unknown", out))
        return rows

    def failure_ip_rows(self) -> list[tuple[str, dict]]:
        ranked = sorted(self.ips.values(), key=lambda i: (-len(i["_accounts"]), -i["failures"]))[:FAILURE_IP_LIMIT]
        rows = []
        for i in ranked:
            out = {k: v for k, v in i.items() if not k.startswith("_")}
            out["accounts"] = len(i["_accounts"])
            out["users"] = _top(i["_accounts"], 10)
            out["countries"] = _top(i["_countries"])
            out["succeeded"] = self.success_ips.get(i["ip"], 0)
            rows.append((i["ip"], out))
        return rows


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _at(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_log(ctx: RunContext) -> events.Ingest:
    """Bring the tenant's stored sign-ins up to date (``events.ingest``). The legacy sign-in collector
    calls this too; whichever runs second reads only the few hours since."""
    return events.ingest(ctx, events.SIGNIN_LOG, LOG_DAYS, SIGNIN_PAGES, retries=QUERY_RETRIES,
                         deadline=lambda: deadline())


def report_gaps(ctx: RunContext, got: events.Ingest, start: datetime, end: datetime, what: str) -> list:
    """Warn about anything between ``start`` and ``end`` this run couldn't read; returns those ranges."""
    missing = got.coverage.missing(int(start.timestamp()), int(end.timestamp()))
    ranges = ", ".join(f"{_at(lo)} to {_at(hi)}" for lo, hi in missing[:3]) + (" and more" if len(missing) > 3
                                                                                  else "")
    if got.gave_up:
        ctx.log(f"Gave up on the sign-in log after {FALLBACK_SECONDS // 60} minutes")
        ctx.warn(f"Microsoft's sign-in log was too slow to answer; sign-ins from {ranges or 'earlier'} are missing "
                 f"from {what} for now, and the next run reads them first")
    elif got.failed_windows:
        ctx.warn(f"Microsoft's sign-in log timed out for {plural(got.failed_windows, 'one-day window')}; those "
                 f"sign-ins are missing from {what} until the next run reads them")
    elif got.budget_hit and missing:
        ctx.truncated = True
        ctx.warn(f"More than {SIGNIN_PAGES * PAGE_SIZE:,} sign-ins to read in one run; sign-ins from {ranges} are "
                 f"missing from {what} for now, and the next run carries on from there")
    elif got.budget_hit:
        ctx.log("Older sign-ins are still being read into the activity history; the next run carries on")
    return missing


@register
class SignIns(Collector):
    key = "sign_ins"
    title = "Sign-in activity"
    description = (f"Interactive sign-ins in the last {signins.WINDOW_DAYS} days, summarised by account, country, "
                   "app and failure reason (needs Entra ID P1; sign-in risk needs P2).")
    permissions = ("AuditLog.Read.All",)

    def collect(self, ctx: RunContext) -> None:
        if ctx.capabilities.get("entra_p1") is False:
            ctx.log("Tenant has no Entra ID P1; the sign-in log isn't available")
            ctx.objects("sign_in_status", [("status", {"available": False, "reason": "licence"})])
            return
        now = events.clock().replace(microsecond=0)
        start = now - timedelta(days=signins.WINDOW_DAYS)
        since = _iso(start)
        p2 = ctx.capabilities.get("entra_p2")
        try:
            got = read_log(ctx)
        except (LicenseRequired, PermissionMissing) as exc:
            ctx.warn(f"The sign-in log isn't available: {exc.message}")
            reason = "licence" if isinstance(exc, LicenseRequired) else "permission"
            ctx.objects("sign_in_status", [("status", {"available": False, "reason": reason})])
            return
        truncated = bool(report_gaps(ctx, got, start, now, "the summary"))
        summary = Summary(risk_visible=p2 is not False)
        for s in events.signins_between(ctx.conn, ctx.tenant["id"], start, now):
            summary.add(s)
        ctx.log(f"Read {plural(got.read, 'sign-in')} from Microsoft ({got.new:,} new) in "
                f"{plural(got.pages, 'page')}")
        # Without P2 Microsoft reports risk as "hidden". When the licence isn't known, trust what came back.
        risk_visible = p2 is True or (p2 is None and summary.risk_seen)
        if not risk_visible:
            summary.risky.clear()
            for a in summary.accounts.values():
                a["risky"] = 0
        accounts = summary.account_rows()
        history = got.coverage.detailed_from
        ctx.objects("sign_in_status", [("status", {
            "available": True, "since": since, "days": signins.WINDOW_DAYS, "signIns": summary.total,
            "truncated": truncated, "riskVisible": risk_visible,
            "historyFrom": _at(history) if history is not None else None})])
        ctx.objects("sign_in_account", accounts)
        ctx.objects("sign_in_location", summary.location_rows())
        ctx.objects("sign_in_country", summary.counted(summary.countries))
        ctx.objects("sign_in_app", summary.counted(summary.apps))
        ctx.objects("sign_in_failure", summary.counted(summary.failures))
        ctx.objects("sign_in_failure_ip", summary.failure_ip_rows())
        ctx.objects("risky_sign_in", summary.risky)
        data = [a for _, a in accounts]
        ctx.metric("signins.total", summary.total)
        ctx.metric("signins.failed", sum(a["failed"] for a in data))
        ctx.metric("signins.single_factor", sum(a["singleFactor"] for a in data))
        ctx.metric("signins.repeated_failures",
                   sum(1 for a in data if a["failed"] >= signins.REPEATED_FAILURES))
        if risk_visible:
            ctx.metric("signins.risky", len(summary.risky))
        ctx.log(f"Summarised {plural(summary.total, 'sign-in')} by {plural(len(data), 'account')}")
