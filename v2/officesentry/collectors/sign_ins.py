"""Sign-in activity: the last week of interactive sign-ins, summarised.

Reads the Entra ID sign-in log (beta, for ``authenticationRequirement``: whether
a sign-in needed MFA or only a password), which needs Entra ID P1. Pages are
summarised as they arrive and the raw log is never stored: one row per account,
per account and country, per country, per app, per failure reason and per
address that failed, plus the risky sign-ins themselves when the tenant has
Entra ID P2 (Identity Protection). Without P2 Microsoft hides sign-in risk.

Microsoft's sign-in log can time out on busy tenants. When the one big query
fails with a timeout or server error (after the Graph client's retries), the
collector carries on from the oldest sign-in it already has, one day at a time,
so the summary is the same as the big query would have given. Each query gets one
retry on a timeout (the smaller ones are the fallback), and the fallback stops
after ``FALLBACK_SECONDS``: when the log is down for the whole tenant, every
window times out, and without a limit the run would hold a worker thread for
hours.

Legacy-protocol sign-ins keep their own 30-day query in ``mail_usage.py``; this
one covers every client for a shorter window, so the two don't share a fetch.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import UTC, datetime, timedelta

from .. import signins
from ..graph import GraphError, LicenseRequired, PermissionMissing, is_transient
from ..text import plural
from .base import Collector, RunContext, register

PAGE_SIZE = 1000
SIGNIN_PAGES = 50  # 50,000 sign-ins; the most recent come first
FAILURE_IP_LIMIT = 200
RISKY_LIMIT = 500
FALLBACK_WINDOW = timedelta(days=1)
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
        requirement = s.get("authenticationRequirement")
        if requirement == "multiFactorAuthentication":
            return True
        if requirement == "singleFactorAuthentication":
            return False
        detail = _text((s.get("status") or {}).get("additionalDetails")) if isinstance(s.get("status"), dict) else ""
        return True if "mfa" in detail.lower() else None

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


class Reader:
    """Pages the sign-in log into a Summary, newest first, across one or more queries.

    It remembers the oldest time seen and the sign-ins at exactly that time, so
    a later query ending at that time (inclusive) neither misses nor
    double-counts anything. ``pages`` counts across queries, so the cap holds.
    """

    def __init__(self, ctx: RunContext, summary: Summary):
        self.ctx = ctx
        self.summary = summary
        self.pages = 0
        self.truncated = False
        self.oldest: str | None = None
        self.at_oldest: set[str] = set()

    def read(self, flt: str) -> None:
        url: str | None = "/auditLogs/signIns"
        params: dict | None = {"$filter": flt, "$top": str(PAGE_SIZE)}
        while url:
            if self.pages >= SIGNIN_PAGES:
                self.truncated = True
                return
            data = self.ctx.graph.get(url, params, version="beta", transient_retries=QUERY_RETRIES)
            params = None
            self.pages += 1
            for s in data.get("value") or []:
                self._add(s)
            url = data.get("@odata.nextLink")

    def _add(self, s) -> None:
        when = _text(s.get("createdDateTime")) if isinstance(s, dict) else ""
        sid = _text(s.get("id")) if isinstance(s, dict) else ""
        if when and when == self.oldest:
            if sid in self.at_oldest:
                return
            if sid:
                self.at_oldest.add(sid)
        elif when and (self.oldest is None or when < self.oldest):
            self.oldest, self.at_oldest = when, ({sid} if sid else set())
        self.summary.add(s)

    def read_by_day(self, since: datetime, failure: GraphError) -> None:
        """Read from the oldest sign-in so far back to ``since`` in one-day windows, newest first.

        A window that still times out is skipped with a warning, and so is every
        window left once ``FALLBACK_SECONDS`` have gone; if nothing was read at
        all, the original failure is raised.
        """
        expired = deadline()
        end, upper = datetime.now(UTC), ""
        if self.oldest:
            try:
                end = datetime.fromisoformat(self.oldest.replace("Z", "+00:00"))
                upper = f" and createdDateTime le {self.oldest}"
            except ValueError:
                pass
        failed = 0
        while end > since and not self.truncated:
            if expired():
                self.ctx.log(f"Gave up on the sign-in log after {FALLBACK_SECONDS // 60} minutes")
                self.ctx.warn(f"Microsoft's sign-in log was too slow to answer; sign-ins before {_iso(end)} "
                              "are missing from the summary")
                break
            start = max(end - FALLBACK_WINDOW, since)
            try:
                self.read(f"createdDateTime ge {_iso(start)}{upper}")
            except GraphError as exc:
                if not is_transient(exc):
                    raise
                failed += 1
                self.ctx.log(f"Sign-ins from {_iso(start)} to {_iso(end)} failed too: {exc}")
            upper = f" and createdDateTime lt {_iso(start)}"
            end = start
        if not self.pages:
            raise failure
        if failed:
            self.ctx.warn(f"Microsoft's sign-in log timed out for {plural(failed, 'one-day window')}; "
                          "those sign-ins are missing from the summary")


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
        start = (datetime.now(UTC) - timedelta(days=signins.WINDOW_DAYS)).replace(microsecond=0)
        since = _iso(start)
        p2 = ctx.capabilities.get("entra_p2")
        summary = Summary(risk_visible=p2 is not False)
        reader = Reader(ctx, summary)
        try:
            try:
                reader.read(f"createdDateTime ge {since}")
            except GraphError as exc:
                if not is_transient(exc):
                    raise
                ctx.log(f"The sign-in log query failed ({exc}); reading it a day at a time instead")
                reader.read_by_day(start, exc)
        except (LicenseRequired, PermissionMissing) as exc:
            ctx.warn(f"The sign-in log isn't available: {exc.message}")
            reason = "licence" if isinstance(exc, LicenseRequired) else "permission"
            ctx.objects("sign_in_status", [("status", {"available": False, "reason": reason})])
            return
        truncated = reader.truncated
        if truncated:
            ctx.truncated = True
            ctx.warn(f"More than {SIGNIN_PAGES * PAGE_SIZE:,} sign-ins in {signins.WINDOW_DAYS} days; summarised "
                     "the most recent ones")
        # Without P2 Microsoft reports risk as "hidden". When the licence isn't known, trust what came back.
        risk_visible = p2 is True or (p2 is None and summary.risk_seen)
        if not risk_visible:
            summary.risky.clear()
            for a in summary.accounts.values():
                a["risky"] = 0
        accounts = summary.account_rows()
        ctx.objects("sign_in_status", [("status", {
            "available": True, "since": since, "days": signins.WINDOW_DAYS, "signIns": summary.total,
            "truncated": truncated, "riskVisible": risk_visible})])
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
