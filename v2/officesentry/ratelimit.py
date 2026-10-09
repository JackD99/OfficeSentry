"""Microsoft's limits shared by every collector thread, and per-run request telemetry.

Microsoft throttles per app (across every tenant), per app per tenant, or per
report API, never per thread: ``signInActivity`` allows 10 requests a minute
for the whole app, sign-in and audit logs 5 per 10 seconds per tenant. So the
worker has one process-wide :data:`LIMITER`. Before each request, Graph and
Exchange clients take units from a token bucket for each limit the endpoint
counts against (``endpoints.py`` lists them); a bucket refills continuously at
Microsoft's documented rate and starts full, so a quiet night bursts. When
Microsoft answers 429 anyway, the endpoint's buckets are paused until its
``Retry-After``, so the other threads wait too instead of spending the same
quota. ``x-ms-throttle-scope`` (when Graph sends it) says whether the app-wide
or the per-tenant buckets are the ones to pause.

Clients made without a limiter (tests, the portal's connection check, the
live test) aren't paced.

:class:`Telemetry` counts one run's requests per endpoint: calls, 429s, other
failures, time spent waiting for retries and for the limiter, and the highest
``x-ms-throttle-limit-percentage`` Graph reported. The worker stores it with the
run (:func:`save`) and the run page shows it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .db import transaction
from .endpoints import LIMITS, Endpoint

# Pause at most this long on one 429, whatever Retry-After says (the clients cap their own sleep the same way).
MAX_PAUSE = 300


@dataclass(frozen=True)
class Claim:
    """Take ``cost`` units from the bucket ``key``, which holds ``units`` per ``seconds`` (None: unpaced)."""

    key: str
    units: int | None
    seconds: float
    cost: int = 1
    scope: str = "tenant"


def claims(endpoint: Endpoint | None, app: str, tenant: str, *, graph: bool = True) -> list[Claim]:
    """The buckets one request to ``endpoint`` takes from. Every Graph request also counts against ``graph``."""
    names = list(endpoint.limits if endpoint else ())
    if graph and "graph" not in names:
        names.append("graph")
    out = []
    for name in names:
        limit = LIMITS[name]
        key = {"app": f"{name}|{app}", "tenant": f"{name}|{app}|{tenant}",
               "api": f"{name}|{app}|{tenant}|{endpoint.path if endpoint else ''}"}[limit.scope]
        cost = endpoint.cost if endpoint and limit.resource_units else 1
        out.append(Claim(key, limit.units, limit.seconds, cost, limit.scope))
    return out


def to_pause(taken: list[Claim], throttle_scope: str = "") -> list[str]:
    """The buckets a 429 on a request that took ``taken`` should pause.

    ``x-ms-throttle-scope`` looks like ``Tenant_Application/ReadWrite/<app>/<tenant>``: an ``Application``
    scope pauses the app-wide buckets, the others the per-tenant ones. Without it, every bucket the
    endpoint has besides the general ``graph`` one (pausing that would stop every tenant's collection).
    """
    specific = [c for c in taken if not c.key.startswith("graph|")]
    scope = throttle_scope.split("/", 1)[0].lower()
    if scope == "application":
        picked = [c for c in specific if c.scope == "app"]
    elif scope in ("tenant", "tenant_application"):
        picked = [c for c in specific if c.scope != "app"]
    else:
        picked = specific
    return [c.key for c in picked or specific]


class _Bucket:
    __slots__ = ("units", "seconds", "tokens", "updated", "paused_until")

    def __init__(self, units: int | None, seconds: float, now: float):
        self.units, self.seconds = units, seconds
        self.tokens = float(units or 0)
        self.updated = now
        self.paused_until = 0.0

    def refill(self, now: float) -> None:
        if self.units:
            rate = self.units / self.seconds
            self.tokens = min(float(self.units), self.tokens + (now - self.updated) * rate)
        self.updated = now

    def wait(self, cost: int, now: float) -> float:
        """Seconds until ``cost`` units are free and the bucket isn't paused (0: now)."""
        wait = max(0.0, self.paused_until - now)
        if self.units:
            needed = min(cost, self.units)  # a request costing more than the bucket holds waits for a full one
            if self.tokens < needed:
                wait = max(wait, (needed - self.tokens) * self.seconds / self.units)
        return wait


class Limiter:
    """Token buckets shared by every thread in the process, keyed by ``Claim.key``."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}

    def _bucket(self, claim: Claim, now: float) -> _Bucket:
        b = self._buckets.get(claim.key)
        if b is None:
            b = self._buckets[claim.key] = _Bucket(claim.units, claim.seconds, now)
        else:
            b.refill(now)
        return b

    def acquire(self, wanted: Iterable[Claim]) -> float:
        """Block until every claim's units are free, take them, and return the seconds waited."""
        wanted = list(wanted)
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                buckets = [(self._bucket(c, now), c.cost) for c in wanted]
                wait = max((b.wait(cost, now) for b, cost in buckets), default=0.0)
                if wait <= 0:
                    for b, cost in buckets:
                        if b.units:
                            b.tokens -= min(cost, b.units)
                    return waited
            self._sleep(wait)
            waited += wait

    def pause(self, keys: Iterable[str], seconds: float) -> None:
        """Hold every request to these buckets for ``seconds`` (after a 429), from every thread."""
        seconds = min(max(seconds, 0.0), MAX_PAUSE)
        with self._lock:
            now = self._clock()
            for key in keys:
                b = self._buckets.get(key)
                if b is not None:
                    b.paused_until = max(b.paused_until, now + seconds)


LIMITER = Limiter()  # the worker's; one per process


@dataclass
class EndpointStats:
    limit: str = ""  # the endpoint's first specific limit, or "" for Graph's general one
    calls: int = 0  # HTTP requests sent, retries included
    throttled: int = 0  # 429 answers
    errors: int = 0  # 5xx answers and network failures
    retry_wait: float = 0.0  # seconds slept before retrying a 429 or 5xx
    limiter_wait: float = 0.0  # seconds held by the shared limiter
    units: int = 0  # resource units used (SharePoint, directory), from the catalogue's costs
    throttle_pct: float | None = None  # highest x-ms-throttle-limit-percentage seen


class Telemetry:
    """One run's requests to Microsoft, per endpoint (``endpoints.graph_key`` or ``"Exchange <cmdlet>"``)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.endpoints: dict[str, EndpointStats] = {}

    def _stats(self, key: str, endpoint: Endpoint | None) -> EndpointStats:
        stats = self.endpoints.get(key)
        if stats is None:
            limit = next((n for n in (endpoint.limits if endpoint else ()) if n != "graph"), "")
            stats = self.endpoints[key] = EndpointStats(limit=limit)
        return stats

    def request(self, key: str, endpoint: Endpoint | None, *, status: int, limiter_wait: float = 0.0,
                retry_wait: float = 0.0, headers=None) -> None:
        """Count one HTTP request (status 0: no answer) and the waits before and after it."""
        with self._lock:
            s = self._stats(key, endpoint)
            s.calls += 1
            s.throttled += status == 429
            s.errors += status == 0 or status >= 500
            s.retry_wait += retry_wait
            s.limiter_wait += limiter_wait
            if endpoint is not None and any(LIMITS[n].resource_units for n in endpoint.limits):
                s.units += endpoint.cost
            pct = _float((headers or {}).get("x-ms-throttle-limit-percentage"))
            if pct is not None and (s.throttle_pct is None or pct > s.throttle_pct):
                s.throttle_pct = pct

    def totals(self) -> dict:
        with self._lock:
            stats = list(self.endpoints.values())
        return {"requests": sum(s.calls for s in stats), "throttled": sum(s.throttled for s in stats),
                "wait_seconds": round(sum(s.retry_wait + s.limiter_wait for s in stats), 3)}


def _float(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def save(conn, run_id: int, telemetry: Telemetry) -> None:
    """Store a run's telemetry: totals on ``runs``, one ``run_requests`` row per endpoint."""
    rows = [(run_id, key, s.limit, s.calls, s.throttled, s.errors, round(s.retry_wait, 3),
             round(s.limiter_wait, 3), s.units, s.throttle_pct) for key, s in sorted(telemetry.endpoints.items())]
    totals = telemetry.totals()
    with transaction(conn):
        conn.execute("UPDATE runs SET requests = ?, throttled = ?, wait_seconds = ? WHERE id = ?",
                     (totals["requests"], totals["throttled"], totals["wait_seconds"], run_id))
        conn.executemany("INSERT OR REPLACE INTO run_requests (run_id, endpoint, throttle_limit, calls, throttled,"
                         " errors, retry_wait, limiter_wait, units, throttle_pct) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
