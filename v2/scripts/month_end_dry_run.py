"""A month-end dry run: one night's collection and the month-end that follows, for N clients, against
Microsoft 365 simulated at the HTTP level.

    cd v2
    python scripts/month_end_dry_run.py --data-dir /tmp/dry-run --tenants 200

Nothing here talks to Microsoft. Every request the worker makes goes through ``requests`` as it would in
production and is answered by :class:`Microsoft`: sign-in (as MSAL calls it), Graph, SharePoint's REST API
and the Exchange admin API answer with the recorded-style responses in ``tests/`` (``tests/seed.py``), and
the email goes out through Graph ``sendMail``. On the way, :class:`Microsoft` enforces Microsoft's documented
limits (``endpoints.LIMITS``, a strict sliding window per app, tenant or API) with 429s and ``Retry-After``,
throws in Microsoft's random 429s and 503s, and makes a share of the clients only partly collect:

- ``consent_removed``: sign-in refused (the client removed the app);
- ``no_exchange_role``: Exchange refuses the app (no Global Reader);
- ``no_p1``: no Entra ID P1 (sign-in logs, last sign-in times and risk need it);
- ``permission_missing``: one permission never approved (Secure Score);
- ``sharepoint_down``: SharePoint keeps answering 503;
- ``slow``: every answer takes seconds.

Email goes through Graph ``sendMail`` from the MSP's mailbox, at Exchange Online's sending limit of 30
messages a minute per mailbox (more is answered 429 with ``Retry-After``).

1. **Night.** The worker (``worker.run_worker``, its collector and task threads, the shared limiter) collects
   every client for the night's slot. Microsoft's waits (``Retry-After``, the limiter) run ``--scale`` times
   faster than the clock, so a night of throttling takes minutes.
2. **Month-end.** Report plans (send on approval, automatic, portal only) with the contacts real clients have
   (none, one, more than one email can carry). An admin opens the month-end queue, approves every client it
   marks Ready, and the worker renders, freezes and emails them; the automatic plan's emails go out on their
   timer. Times, failures, email sizes and the worker's memory are recorded.
3. **Report.** What happened, as JSON on stdout (and in ``--json``): run outcomes and throttling for the
   night; for the month-end the queue's flags, approval times and failures, the emails Microsoft took and
   refused, the largest files and the worker's memory every half minute.

``--month-end-only`` skips the night and runs the month-end on a database made by ``scale_data.py`` (realistic
tenant sizes and history), with every client on its plan.

Development only: it imports the test fixtures, which aren't in the Docker image.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import resource
import sys
import threading
import time
from collections import Counter, deque
from contextlib import ExitStack
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

V2 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(V2))

import requests  # noqa: E402
import responses  # noqa: E402
from requests.structures import CaseInsensitiveDict  # noqa: E402

from officesentry import endpoints, ratelimit, store, worker  # noqa: E402
from officesentry.certs import generate_certificate  # noqa: E402
from officesentry.db import connect, migrate, transaction, utcnow  # noqa: E402

USERNAME, PASSWORD = "dryrun", "a throwaway password for the dry run"  # noqa: S105 (a throwaway database)
SENDER = "reports@msp.example"
APP_ID = "11111111-2222-3333-4444-555555555555"
MAIL_APP_ID = "99999999-2222-3333-4444-555555555555"
MAIL_TENANT = "msp.onmicrosoft.com"

TROUBLES = ("consent_removed", "no_exchange_role", "no_p1", "permission_missing", "sharepoint_down", "slow")
# Every Nth client has the trouble (offset so they don't coincide much).
TROUBLE_EVERY = {
    "consent_removed": (40, 7),
    "no_exchange_role": (25, 3),
    "no_p1": (20, 5),
    "permission_missing": (33, 13),
    "sharepoint_down": (30, 11),
    "slow": (50, 17),
}
NO_P1_PATHS = re.compile(r"/(auditLogs/signIns|identityProtection/|reports/servicePrincipalSignInActivities)")
MAIL_PER_MINUTE = 30  # Exchange Online: messages a minute from one mailbox
EXCHANGE_PER_10S = 30  # Microsoft documents no rate for the Exchange admin API; this is the simulated one


def trouble_of(n: int) -> str:
    """The trouble the n-th client (1-based) has, or ""."""
    for kind, (every, offset) in TROUBLE_EVERY.items():
        if n % every == offset:
            return kind
    return ""


class Clock:
    """Microsoft's waits run ``scale`` times faster than the wall clock: a 30-second ``Retry-After`` takes half
    a second at 60. Both the simulated Microsoft and the worker's limiter and retries read this clock."""

    def __init__(self, scale: float):
        self.scale = scale
        self.started = time.monotonic()

    def now(self) -> float:
        return (time.monotonic() - self.started) * self.scale

    def sleep(self, seconds: float) -> None:
        time.sleep(max(seconds, 0.0) / self.scale)


def _response(request, status: int, body=None, headers=None) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps(body).encode() if body is not None else b""
    resp.headers = CaseInsensitiveDict({"Content-Type": "application/json", **(headers or {})})
    resp.url = request.url
    resp.request = request
    resp.encoding = "utf-8"
    resp.reason = {200: "OK", 202: "Accepted", 429: "Too Many Requests", 503: "Service Unavailable"}.get(status, "")
    return resp


class Microsoft:
    """Microsoft 365's side of every request: limits, noise and each client's trouble. Thread-safe."""

    def __init__(self, clock: Clock, troubles: dict[str, str], *, noise: float = 0.005, errors: float = 0.002):
        self.clock = clock
        self.troubles = troubles  # tenant ref -> trouble
        self.noise, self.errors = noise, errors
        self.rng = random.Random(20261031)  # noqa: S311 (simulation)
        self.lock = threading.Lock()
        self.windows: dict[str, deque] = {}
        self.stats: Counter = Counter()
        self.by_limit: Counter = Counter()
        self.mail: list[dict] = []
        self.mail_window: deque = deque()
        self.mail_down_until = -1.0  # Microsoft's clock: sendMail answers 503 until then

    # -- Limits ---------------------------------------------------------------------------------

    def _over(self, claims: list[ratelimit.Claim], now: float) -> tuple[float, str]:
        """Seconds until every claim fits Microsoft's window (0: now), and the scope that's over. Records the
        request when it fits."""
        wait, scope = 0.0, ""
        for c in claims:
            if not c.units:
                continue
            window = self.windows.setdefault(c.key, deque())
            while window and window[0][0] <= now - c.seconds:
                window.popleft()
            used = sum(cost for _, cost in window)
            if used + c.cost > c.units:
                # Until enough of the oldest requests leave the window.
                free, need = 0.0, used + c.cost - c.units
                for at, cost in window:
                    need -= cost
                    if need <= 0:
                        free = at + c.seconds - now
                        break
                if free > wait:
                    wait, scope = free, c.scope
        if wait <= 0:
            for c in claims:
                if c.units:
                    self.windows[c.key].append((now, c.cost))
        return wait, scope

    def _throttled(self, request, wait: float, scope: str, limit: str) -> requests.Response:
        self.stats["429 (limit)"] += 1
        self.by_limit[limit] += 1
        header = "Application" if scope == "app" else "Tenant_Application"
        return _response(
            request,
            429,
            {"error": {"code": "TooManyRequests", "message": "Too many requests"}},
            {"Retry-After": str(max(1, math.ceil(wait))), "x-ms-throttle-scope": f"{header}/ReadOnly/{APP_ID}/t"},
        )

    # -- Requests -------------------------------------------------------------------------------

    def intercept(self, request) -> requests.Response | None:
        """An answer of Microsoft's own (a 429, an error, a refusal), or None to answer from the fixtures."""
        parts = urlsplit(request.url)
        host = parts.hostname or ""
        with self.lock:
            self.stats["requests"] += 1
            now = self.clock.now()
            if host.startswith("login."):
                return self._sign_in(request, parts)
            token = (request.headers.get("Authorization") or "").removeprefix("Bearer ")
            tenant = token.split("|")[1] if token.count("|") >= 2 else ""
            if parts.path.endswith("/sendMail"):
                return self._send_mail(request, now)
            trouble = self.troubles.get(tenant, "")
            latency = 3.0 if trouble == "slow" else 0.0
            if host.endswith(".sharepoint.com"):
                if trouble == "sharepoint_down":
                    self.stats["503 (SharePoint down)"] += 1
                    return _response(request, 503, {"error": {"code": "serviceUnavailable"}})
                endpoint = endpoints.sharepoint(request.url)
                claims = ratelimit.claims(endpoint, APP_ID, tenant, graph=False)
                limit = next((n for n in (endpoint.limits if endpoint else ()) if n != "graph"), "sharepoint")
            elif host.startswith("outlook."):
                if trouble == "no_exchange_role":
                    self.stats["403 (no Exchange role)"] += 1
                    return _response(
                        request, 403, {"error": {"code": "Forbidden", "message": "The role assigned isn't valid."}}
                    )
                claims = [ratelimit.Claim(f"exchange|{tenant}", EXCHANGE_PER_10S, 10)]
                limit = "exchange"
            else:
                path = parts.path
                if trouble == "no_p1" and (NO_P1_PATHS.search(path) or "signInActivity" in unquote(parts.query or "")):
                    self.stats["403 (no P1)"] += 1
                    return _response(
                        request,
                        403,
                        {
                            "error": {
                                "code": "Authentication_RequestFromNonPremiumTenantOrB2CTenant",
                                "message": "Tenant does not have a SKU required for this feature.",
                            }
                        },
                    )
                if trouble == "permission_missing" and "/security/secureScore" in path:
                    self.stats["403 (permission missing)"] += 1
                    return _response(
                        request,
                        403,
                        {"error": {"code": "Authorization_RequestDenied", "message": "Insufficient privileges."}},
                    )
                endpoint = endpoints.graph(request.url)
                claims = ratelimit.claims(endpoint, APP_ID, tenant)
                limit = next((n for n in (endpoint.limits if endpoint else ()) if n != "graph"), "graph")
            wait, scope = self._over(claims, now)
            if wait > 0:
                return self._throttled(request, wait, scope, limit)
            roll = self.rng.random()
        if latency:
            self.clock.sleep(latency)
        if roll < self.noise:  # Microsoft throttles now and then whatever the documented limit says
            with self.lock:
                self.stats["429 (random)"] += 1
            return _response(
                request,
                429,
                {"error": {"code": "TooManyRequests", "message": "Too many requests"}},
                {
                    "Retry-After": str(self.rng.randint(1, 20)),
                    "x-ms-throttle-scope": f"Tenant_Application/R/{APP_ID}/t",
                },
            )
        if roll < self.noise + self.errors:
            with self.lock:
                self.stats["503 (random)"] += 1
            return _response(request, 503, {"error": {"code": "serviceUnavailable", "message": "Try again"}})
        return None

    def _sign_in(self, request, parts) -> requests.Response | None:
        tenant = parts.path.split("/")[1]
        if request.method == "POST" and self.troubles.get(tenant) == "consent_removed":
            self.stats["sign-in refused"] += 1
            return _response(
                request,
                400,
                {
                    "error": "unauthorized_client",
                    "error_description": f"AADSTS700016: Application with identifier '{APP_ID}' was not found in"
                    f" the directory '{tenant}'.",
                },
            )
        return None

    def _send_mail(self, request, now: float) -> requests.Response:
        if now < self.mail_down_until:
            self.stats["sendMail 503"] += 1
            return _response(request, 503, {"error": {"code": "ErrorServiceUnavailable"}})
        while self.mail_window and self.mail_window[0] <= now - 60:
            self.mail_window.popleft()
        if len(self.mail_window) >= MAIL_PER_MINUTE:
            self.stats["sendMail 429"] += 1
            return _response(
                request,
                429,
                {"error": {"code": "ApplicationThrottled", "message": "Too many messages sent in a short time."}},
                {"Retry-After": str(max(1, math.ceil(self.mail_window[0] + 60 - now)))},
            )
        self.mail_window.append(now)
        message = json.loads(request.body)["message"]
        self.mail.append(
            {
                "at": now,
                "subject": message["subject"],
                "to": len(message["toRecipients"]),
                "bcc": len(message["bccRecipients"]),
                "attachments": [a["name"] for a in message["attachments"] if not a.get("isInline")],
                "bytes": len(request.body),
            }
        )
        self.stats["sendMail sent"] += 1
        return _response(request, 202)


def _token_callback(request):
    """A token naming the tenant and resource it's for, so :class:`Microsoft` knows whose request it is."""
    tenant = urlsplit(request.url).path.split("/")[1]
    form = parse_qs(request.body if isinstance(request.body, str) else request.body.decode())
    resource = urlsplit(form.get("scope", [""])[0]).hostname or ""
    body = {"access_token": f"sim|{tenant}|{resource}", "expires_in": 3600, "token_type": "Bearer"}
    return 200, {}, json.dumps(body)


def mock_sign_in(rsps) -> None:
    login = "https://login.microsoftonline.com"

    def discovery(request):
        tenant = request.url.split("/")[3]
        base = f"{login}/{tenant}"
        meta = {
            "token_endpoint": f"{base}/oauth2/v2.0/token",
            "authorization_endpoint": f"{base}/oauth2/v2.0/authorize",
            "issuer": f"{login}/{tenant}/v2.0",
        }
        return 200, {}, json.dumps(meta)

    rsps.add_callback(
        responses.GET, re.compile(re.escape(login) + r"/[^/]+/v2\.0/\.well-known/openid-configuration"), discovery
    )
    rsps.get(
        re.compile(re.escape(login) + r"/common/discovery/instance"),
        json={
            "tenant_discovery_endpoint": f"{login}/common/v2.0/.well-known/openid-configuration",
            "metadata": [
                {
                    "preferred_network": "login.microsoftonline.com",
                    "preferred_cache": "login.windows.net",
                    "aliases": ["login.microsoftonline.com", "login.windows.net"],
                }
            ],
        },
    )
    rsps.add_callback(responses.POST, re.compile(re.escape(login) + r"/[^/]+/oauth2/v2\.0/token"), _token_callback)


class Simulation:
    """Microsoft 365 at the HTTP level for the duration of a ``with`` block."""

    def __init__(self, microsoft: Microsoft):
        self.microsoft = microsoft
        self._stack = ExitStack()

    def __enter__(self):
        from tests import graphdata as gd
        from tests.seed import mock_tenant

        rsps = self._stack.enter_context(responses.RequestsMock(assert_all_requests_are_fired=False))
        mock_sign_in(rsps)
        mock_tenant(rsps)
        self._stack.enter_context(mock.patch("officesentry.dnscheck.lookup", gd.fake_dns))
        answer = requests.adapters.HTTPAdapter.send  # responses' own, now patched in
        microsoft = self.microsoft

        def send(adapter, request, *args, **kwargs):
            early = microsoft.intercept(request)
            return early if early is not None else answer(adapter, request, *args, **kwargs)

        self._stack.enter_context(mock.patch.object(requests.adapters.HTTPAdapter, "send", send))
        return self

    def __exit__(self, *exc):
        return self._stack.__exit__(*exc)


# -- The worker, with Microsoft's clock ------------------------------------------------------------------


def factories(vault, clock: Clock, limiter: ratelimit.Limiter):
    """``worker.default_graph_factory`` and ``default_exchange_factory``, with retries and the limiter on
    Microsoft's (faster) clock."""
    from officesentry.exchange import ExchangeClient
    from officesentry.graph import GraphClient

    def graph(tenant, connection):
        cloud = worker.connection_cloud(tenant, connection)
        return GraphClient.from_certificate(
            tenant["tenant_ref"],
            connection["client_id"],
            vault.decrypt(connection["key_enc"]),
            connection["thumbprint"],
            cloud=cloud,
            session=worker._session(),
            limiter=limiter,
            sleep=clock.sleep,
        )

    def exchange(tenant, connection):
        cloud = worker.connection_cloud(tenant, connection)
        return ExchangeClient.from_certificate(
            tenant["tenant_ref"],
            connection["client_id"],
            vault.decrypt(connection["key_enc"]),
            connection["thumbprint"],
            cloud=cloud,
            session=worker._session(),
            limiter=limiter,
            sleep=clock.sleep,
        )

    return graph, exchange


def run_worker_until(
    cfg, done, *, graph_factory=None, exchange_factory=None, timeout: float = 4 * 3600, memory: list | None = None
) -> float:
    """Run the worker (collector and task threads, housekeeping) until ``done(conn)``. Returns seconds taken.
    ``memory`` gets (seconds, resident MB) about every half minute."""
    stop = threading.Event()
    started = time.monotonic()
    thread = threading.Thread(
        target=worker.run_worker,
        args=(cfg,),
        kwargs={"graph_factory": graph_factory, "exchange_factory": exchange_factory, "stop": stop},
        daemon=True,
    )
    thread.start()
    conn = connect(cfg.db_path)
    try:
        time.sleep(3)
        while time.monotonic() - started < timeout:
            if done(conn):
                break
            elapsed = time.monotonic() - started
            if memory is not None and (not memory or elapsed - memory[-1][0] >= 30):
                memory.append((round(elapsed), round(rss_mb())))
            time.sleep(2)
    finally:
        conn.close()
        stop.set()
        thread.join(600)
    return time.monotonic() - started


def queues_empty(conn) -> bool:
    runs = conn.execute("SELECT COUNT(*) FROM runs WHERE status IN ('queued', 'running')").fetchone()[0]
    work = conn.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('queued', 'running')").fetchone()[0]
    return runs == 0 and work == 0 and conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] > 0


def month_end_done(conn) -> bool:
    """Every approval and every automatic plan email has been dealt with (sent, or failed and waiting to retry),
    and nothing else is queued."""
    waiting = conn.execute(
        "SELECT COUNT(*) FROM email_schedules s JOIN report_plans p ON p.id = s.plan_id"
        " WHERE p.mode = 'auto' AND s.enabled = 1 AND s.last_status IS NULL"
        " AND s.tenant_id IN (SELECT id FROM tenants WHERE enabled = 1)"
    ).fetchone()[0]
    return waiting == 0 and queues_empty(conn)


def rss_mb() -> float:
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


# -- Setting up the MSP --------------------------------------------------------------------------------


def contacts_for(n: int) -> int:
    """How many contacts the n-th client has: most one or two, a few none, one more than an email can carry."""
    if n % 37 == 0:
        return 0
    if n % 61 == 1:
        return 25
    return 1 + n % 2


def plan_of(n: int, total: int) -> str:
    """Which plan the n-th client is on: most wait for approval, some send on their own, some portal only."""
    if n <= total * 0.7:
        return "approval"
    if n <= total * 0.9:
        return "auto"
    return "portal"


def set_up(db, vault, tenants: int) -> dict[str, str]:
    """An app connection, ``tenants`` consented clients with contacts, plans and mail settings. Returns
    {tenant ref: trouble}."""
    from officesentry import delivery

    troubles = {}
    with transaction(db, immediate=True):
        cid = store.create_connection(db, vault, "Dry run app", generate_certificate("dry-run"), client_id=APP_ID)
        for n in range(1, tenants + 1):
            ref = f"client{n:04}.onmicrosoft.com"
            tid = store.create_tenant(db, f"Client {n:04}", ref, cid)
            db.execute("UPDATE tenants SET consented_at = ? WHERE id = ?", (utcnow(), tid))
            troubles[ref] = trouble_of(n)
            for c in range(contacts_for(n)):
                delivery.add_contact(db, tid, f"Contact {c + 1}", f"it{c + 1}@client{n:04}.example")
        # Two clients paused, as an MSP always has one or two.
        db.execute("UPDATE tenants SET enabled = 0 WHERE id IN (SELECT id FROM tenants ORDER BY id DESC LIMIT 2)")
        delivery.add_contact(db, None, "MSP team", "team@msp.example")
        _mail_settings(db, vault)
        _admin(db)
        add_plans(db, tenants)
    return troubles


def _mail_settings(db, vault) -> None:
    for key, value in {
        "mail_transport": "graph",
        "graph_auth": "secret",
        "graph_tenant": MAIL_TENANT,
        "graph_client_id": MAIL_APP_ID,
        "graph_secret_enc": vault.encrypt("dry-run"),
        "graph_sender": SENDER,
        "msp_name": "Dry Run IT",
    }.items():
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def _admin(db) -> None:
    from werkzeug.security import generate_password_hash

    db.execute(
        "INSERT OR IGNORE INTO users (username, password_hash, role, all_tenants, created_at)"
        " VALUES (?, ?, 'admin', 1, ?)",
        (USERNAME, generate_password_hash(PASSWORD, method="pbkdf2:sha256:1"), utcnow()),
    )


PLANS = {
    "approval": {
        "name": "Monthly security report",
        "reports": ["review"],
        "include_xlsx": 1,
        "mode": "approval",
        "delivery": "email",
        "bcc_team": 1,
    },
    "auto": {
        "name": "Monthly summary (automatic)",
        "reports": ["review-summary", "evidence"],
        "include_xlsx": 0,
        "mode": "auto",
        "delivery": "email",
        "bcc_team": 1,
    },
    "portal": {
        "name": "Client portal only",
        "reports": ["review"],
        "include_xlsx": 0,
        "mode": "approval",
        "delivery": "portal",
        "bcc_team": 0,
    },
}


def add_plans(db, tenants: int | None = None) -> None:
    """The three plans, every client on one (``plan_of``). On a ``scale_data.py`` database, which has its own
    plan, the clients are moved onto these."""
    from officesentry import month_end

    ids = [r["id"] for r in db.execute("SELECT id FROM tenants ORDER BY id")]
    total = tenants or len(ids)
    for kind, values in PLANS.items():
        full = {
            **values,
            "day_of_month": 1,
            "hour": 8,
            "timezone": "UTC",
            "subject": month_end.DEFAULT_SUBJECT,
            "message": "Hello {client}, here is your report for {month}.",
        }
        plan = month_end.get_plan(db, month_end.save_plan(db, full, USERNAME))
        chosen = [tid for n, tid in enumerate(ids, start=1) if plan_of(n, total) == kind]
        month_end.apply_plan(db, plan, chosen, USERNAME, "UTC")


def the_first_comes_round(db) -> None:
    """The automatic plan's emails for this month fall due (applying a plan counts the current month as done)."""
    from officesentry import month_end

    db.execute(
        "UPDATE email_schedules SET last_period = ? WHERE plan_id IN (SELECT id FROM report_plans WHERE mode = 'auto')",
        (month_end.shift_period(month_end.current_period("UTC"), -1),),
    )


def backdate_findings(db) -> None:
    """A night's collection opens every finding; a real month-end has most of them from earlier months. Leave
    every 15th client's findings new, as if something critical turned up this month."""
    db.execute(
        "UPDATE findings SET first_seen = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-45 days') WHERE tenant_id % 15 != 0"
    )


# -- Phases ------------------------------------------------------------------------------------------


def night(cfg, vault, troubles: dict[str, str], scale: float, concurrency: int) -> dict:
    """Collect every client for the night's slot through the simulated Microsoft. Returns what happened."""
    clock = Clock(scale)
    microsoft = Microsoft(clock, troubles)
    limiter = ratelimit.Limiter(clock=clock.now, sleep=clock.sleep)
    graph, exchange = factories(vault, clock, limiter)
    cfg.worker_concurrency = concurrency
    with Simulation(microsoft):
        seconds = run_worker_until(cfg, queues_empty, graph_factory=graph, exchange_factory=exchange)
    db = connect(cfg.db_path)
    try:
        runs = Counter(r["status"] for r in db.execute("SELECT status FROM runs"))
        failed = Counter(
            (r["collector"], (r["error"] or "")[:120])
            for r in db.execute("SELECT collector, error FROM runs WHERE status = 'failed'")
        )
        totals = db.execute(
            "SELECT SUM(requests) AS requests, SUM(throttled) AS throttled, SUM(wait_seconds) AS waited FROM runs"
        ).fetchone()
        busiest = db.execute(
            "SELECT endpoint, SUM(calls) AS calls, SUM(throttled) AS throttled, ROUND(SUM(limiter_wait), 1) AS"
            " limiter_wait, ROUND(SUM(retry_wait), 1) AS retry_wait FROM run_requests GROUP BY endpoint"
            " ORDER BY SUM(limiter_wait) + SUM(retry_wait) DESC LIMIT 12"
        ).fetchall()
        tasks = Counter((r["kind"], r["status"]) for r in db.execute("SELECT kind, status FROM tasks"))
    finally:
        db.close()
    return {
        "seconds": round(seconds, 1),
        "simulated_minutes": round(clock.now() / 60, 1),
        "runs": dict(runs),
        "failed": [{"collector": c, "error": e, "count": n} for (c, e), n in failed.most_common(25)],
        "requests": totals["requests"],
        "throttled": totals["throttled"],
        "waited_simulated_seconds": round(totals["waited"] or 0, 1),
        "busiest": [dict(r) for r in busiest],
        "microsoft": dict(microsoft.stats),
        "microsoft_by_limit": dict(microsoft.by_limit),
        "tasks": {f"{k} {s}": n for (k, s), n in tasks.items()},
    }


def month_end(cfg, *, mail_outage: float = 0.0) -> dict:
    """Open the queue as an admin, approve every Ready client, then run the worker until every approval and
    the automatic plan's emails are done. ``mail_outage``: seconds sendMail answers 503 from the start."""
    from officesentry import month_end as me
    from officesentry.web import create_app

    period = me.current_period("UTC")
    app = create_app(cfg)
    client = app.test_client()
    client.post("/login", data={"username": USERNAME, "password": PASSWORD})
    out: dict = {"period": period}

    started = time.monotonic()
    page = client.get(f"/reporting/{period}")
    out["queue_page_seconds"] = round(time.monotonic() - started, 2)
    out["queue_page_status"] = page.status_code
    db = connect(cfg.db_path)
    try:
        tenants = db.execute("SELECT * FROM tenants ORDER BY id").fetchall()
        rows = me.queue(db, tenants, period)
        out["queue"] = {
            "rows": len(rows),
            "ready": sum(r.ready for r in rows),
            "states": dict(Counter(r.state for r in rows)),
            "flags": dict(Counter(f for r in rows for f in r.flags)),
            "paused": sum(r.paused for r in rows),
        }
        ready = [r.tenant["id"] for r in rows if r.ready]
        flagged_ids = {r.tenant["id"]: r.flags for r in rows if r.flags}
    finally:
        db.close()

    db = connect(cfg.db_path)
    try:
        with transaction(db, immediate=True):
            the_first_comes_round(db)
    finally:
        db.close()
    started = time.monotonic()
    resp = client.post(f"/reporting/{period}/approve", data={"tenants": [str(t) for t in ready]})
    out["approve_post_seconds"] = round(time.monotonic() - started, 2)
    out["approve_post_status"] = resp.status_code

    clock = Clock(1)
    microsoft = Microsoft(clock, {}, noise=0, errors=0)
    microsoft.mail_down_until = mail_outage
    rss_before, memory = rss_mb(), []
    with Simulation(microsoft):
        out["worker_seconds"] = round(run_worker_until(cfg, month_end_done, memory=memory), 1)
    out["rss_mb"] = {"before": round(rss_before), "after": round(rss_mb()), "every_30s": [mb for _, mb in memory]}
    out["mail"] = {
        "sent": len(microsoft.mail),
        "stats": dict(microsoft.stats),
        "largest_kb": round(max((m["bytes"] for m in microsoft.mail), default=0) / 1024),
    }
    db = connect(cfg.db_path)
    try:
        approvals = db.execute(
            "SELECT status, error, (julianday(finished_at) - julianday(started_at)) * 86400 AS secs"
            " FROM tasks WHERE kind = 'approve'"
        ).fetchall()
        secs = sorted(r["secs"] for r in approvals if r["secs"] is not None)
        out["approvals"] = {
            "queued": len(ready),
            "statuses": dict(Counter(r["status"] for r in approvals)),
            "errors": dict(Counter((r["error"] or "")[:160] for r in approvals if r["status"] == "failed")),
            "median_seconds": round(secs[len(secs) // 2], 2) if secs else None,
            "slowest_seconds": round(secs[-1], 2) if secs else None,
        }
        sends = db.execute(
            "SELECT status, error, COUNT(*) AS n FROM email_deliveries WHERE kind = 'report' GROUP BY status, error"
        ).fetchall()
        out["report_emails"] = [
            {"status": r["status"], "error": (r["error"] or "")[:160], "count": r["n"]} for r in sends
        ]
        tenants = db.execute("SELECT * FROM tenants ORDER BY id").fetchall()
        after = me.queue(db, tenants, period)
        out["after"] = {
            "states": dict(Counter(r.state for r in after)),
            "send_failed": sum(1 for r in after if r.error),
            "errors": dict(Counter((r.error or "")[:160] for r in after if r.error)),
        }
        auto = db.execute(
            "SELECT s.last_status, s.attempts, s.retry_at IS NOT NULL AS retrying, COUNT(*) AS n"
            " FROM email_schedules s JOIN report_plans p ON p.id = s.plan_id WHERE p.mode = 'auto'"
            " GROUP BY 1, 2, 3"
        ).fetchall()
        out["automatic_plan"] = [dict(r) for r in auto]
        out["flagged_examples"] = dict(list({str(k): v for k, v in flagged_ids.items()}.items())[:8])
        sizes = db.execute(
            "SELECT filename, LENGTH(data) AS bytes FROM issued_reports ORDER BY LENGTH(data) DESC LIMIT 3"
        ).fetchall()
        out["largest_files"] = [{"file": r["filename"], "kb": round(r["bytes"] / 1024)} for r in sizes]
    finally:
        db.close()
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--tenants", type=int, default=200)
    parser.add_argument("--scale", type=float, default=60, help="how much faster Microsoft's waits run")
    parser.add_argument("--concurrency", type=int, default=4, help="the worker's collector threads")
    parser.add_argument("--month-end-only", action="store_true", help="a scale_data.py database: no night")
    parser.add_argument("--mail-outage", type=float, default=0.0, help="seconds sendMail answers 503 at first")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    import logging

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(threadName)s %(levelname)s %(message)s")
    mock.patch("officesentry.logs.setup", lambda *a, **k: None).start()  # keep the worker's INFO lines quiet

    from officesentry.config import load_config
    from officesentry.crypto import Vault

    cfg = load_config(
        data_dir=args.data_dir, testing=True, secure_cookies=False, require_mfa="off", extra={"disable_csrf": True}
    )
    vault = Vault(cfg.encryption_keys)
    db = connect(cfg.db_path)
    migrate(db)
    result: dict = {"tenants": args.tenants, "when": utcnow()}
    try:
        if args.month_end_only:
            with transaction(db, immediate=True):
                db.execute("DELETE FROM report_plan_tenants")
                db.execute("DELETE FROM email_schedules WHERE plan_id IS NOT NULL")
                db.execute("DELETE FROM report_plans")
                _mail_settings(db, vault)
                _admin(db)
                delivery_team = db.execute("SELECT 1 FROM contacts WHERE tenant_id IS NULL").fetchone()
                if not delivery_team:
                    from officesentry import delivery

                    delivery.add_contact(db, None, "MSP team", "team@msp.example")
                add_plans(db)
                backdate_findings(db)
            troubles = {}
        else:
            if db.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]:
                raise SystemExit("--data-dir must be a new, empty data directory")
            troubles = set_up(db, vault, args.tenants)
    finally:
        db.close()
    result["troubles"] = dict(Counter(t for t in troubles.values() if t))
    if not args.month_end_only:
        print(f"Night: collecting {args.tenants} clients ...", flush=True)
        result["night"] = night(cfg, vault, troubles, args.scale, args.concurrency)
        print(json.dumps(result["night"], indent=2), flush=True)
        db = connect(cfg.db_path)
        try:
            with transaction(db, immediate=True):
                backdate_findings(db)
        finally:
            db.close()
    print("Month-end ...", flush=True)
    result["month_end"] = month_end(cfg, mail_outage=args.mail_outage)
    print(json.dumps(result["month_end"], indent=2, default=str), flush=True)
    if args.json:
        args.json.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
