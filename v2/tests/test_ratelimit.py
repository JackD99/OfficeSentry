"""The shared limiter, request telemetry and the endpoint catalogue.

Graph is simulated with ``responses`` at the HTTP level; the threaded tests use
real time with limits shrunk so they finish in a second or two.
"""

import threading
import time
from urllib.parse import urlsplit

import pytest
import responses

from officesentry import endpoints, ratelimit, store
from officesentry.collectors import REGISTRY
from officesentry.endpoints import LIMITS, Limit
from officesentry.exchange import ExchangeClient
from officesentry.graph import GRAPH_ROOT, USER_AGENT, GraphClient, GraphError
from officesentry.ratelimit import Claim, Limiter, Telemetry
from officesentry.worker import execute_run
from tests import replay
from tests.conftest import login, make_user
from tests.seed import seed_tenant

USERS = f"{GRAPH_ROOT}/v1.0/users"
SIGN_INS = f"{GRAPH_ROOT}/v1.0/auditLogs/signIns"
ACTIVITY = {"$select": "id,signInActivity", "$top": "500"}


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


# -- The bucket arithmetic, on a fake clock ------------------------------------------------


def test_bucket_bursts_then_paces_at_microsofts_rate():
    t = FakeTime()
    limiter = Limiter(t.clock, t.sleep)
    claim = Claim("signin_activity|app", 10, 60)  # 10 a minute for the whole app
    for _ in range(10):
        assert limiter.acquire([claim]) == 0  # a quiet bucket starts full
    assert limiter.acquire([claim]) == pytest.approx(6)  # then one every 6 seconds
    assert limiter.acquire([claim]) == pytest.approx(6)


def test_every_bucket_must_have_room():
    t = FakeTime()
    limiter = Limiter(t.clock, t.sleep)
    tenant = Claim("identity_reports_tenant|app|t1", 5, 10)
    app = Claim("identity_reports|app", 122, 10)
    for _ in range(5):
        limiter.acquire([tenant, app])
    assert limiter.acquire([tenant, app]) == pytest.approx(2)  # the tenant's 5 per 10 s is the tighter one
    other = Claim("identity_reports_tenant|app|t2", 5, 10)
    assert limiter.acquire([other, app]) == 0  # another tenant has its own


def test_resource_units_and_requests_bigger_than_the_bucket():
    t = FakeTime()
    limiter = Limiter(t.clock, t.sleep)
    assert limiter.acquire([Claim("sp", 10, 10, cost=5)]) == 0
    assert limiter.acquire([Claim("sp", 10, 10, cost=5)]) == 0
    assert limiter.acquire([Claim("sp", 10, 10, cost=5)]) == pytest.approx(5)
    assert limiter.acquire([Claim("tiny", 2, 10, cost=5)]) == 0  # costs more than it holds: waits for a full bucket
    assert limiter.acquire([Claim("unpaced", None, 0)]) == 0


def test_pause_holds_a_bucket():
    t = FakeTime()
    limiter = Limiter(t.clock, t.sleep)
    claim = Claim("exchange|app|t1", None, 0)
    limiter.acquire([claim])
    limiter.pause([claim.key], 30)
    limiter.pause([claim.key], 5)  # a shorter pause never shortens a longer one
    assert limiter.acquire([claim]) == pytest.approx(30)
    limiter.pause([claim.key], 10_000)
    assert limiter.acquire([claim]) == pytest.approx(ratelimit.MAX_PAUSE)


def test_claims_follow_the_catalogue():
    users = endpoints.graph(USERS, ACTIVITY)
    keys = [c.key for c in ratelimit.claims(users, "app1", "t1")]
    assert keys == ["signin_activity|app1", "directory|app1|t1", "graph|app1"]
    assert ratelimit.claims(users, "app1", "t1")[1].cost == 2  # GET users is 2 directory resource units
    report = endpoints.graph(f"{GRAPH_ROOT}/beta/reports/getM365AppUserDetail(period='D30')")
    assert [c.key for c in ratelimit.claims(report, "a", "t")][
        0
    ] == "usage_reports|a|t|/reports/getM365AppUserDetail(period={period})"  # each report API has its own
    # A 429 pauses the endpoint's own buckets; x-ms-throttle-scope picks app-wide or per-tenant ones.
    logs = ratelimit.claims(endpoints.graph(SIGN_INS), "a", "t")
    assert ratelimit.to_pause(logs) == ["identity_reports_tenant|a|t", "identity_reports|a"]
    assert ratelimit.to_pause(logs, "Tenant_Application/ReadWrite/a/t") == ["identity_reports_tenant|a|t"]
    assert ratelimit.to_pause(logs, "Application/ReadWrite/a") == ["identity_reports|a"]
    assert ratelimit.to_pause(ratelimit.claims(endpoints.graph(f"{GRAPH_ROOT}/v1.0/groups"), "a", "t")) == [
        "directory|a|t"
    ]  # never Graph's general bucket, which every tenant shares


# -- Several worker threads against simulated Graph -----------------------------------------


def _threads(n, target):
    errors = []

    def run(i):
        try:
            target(i)
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(30)
    assert not errors, errors


def test_threads_share_the_app_wide_limit(monkeypatch):
    """Four tenants' threads reading signInActivity together stay within one app-wide rate."""
    monkeypatch.setitem(LIMITS, "signin_activity", Limit("signin_activity", 4, 0.4, "app", "test"))
    limiter = Limiter()
    times = []
    with responses.RequestsMock() as rsps:
        rsps.add_callback(
            responses.GET, USERS, callback=lambda req: (times.append(time.monotonic()), (200, {}, '{"value": []}'))[1]
        )

        def collect(i):
            graph = GraphClient(lambda: "t", limiter=limiter, app="app", tenant=f"tenant{i}")
            for _ in range(3):
                graph.get("/users", ACTIVITY)

        start = time.monotonic()
        _threads(4, collect)
    assert len(times) == 12
    # A burst of 4, then 10 a second: the 12th request can't come before 0.8 s.
    assert times[-1] - start >= 0.75
    for i, t in enumerate(sorted(times)):
        assert i + 1 <= 4 + (t - start) * 10 + 1  # never ahead of the bucket (one request of slack)


def test_a_429_pauses_every_thread_on_that_limit(monkeypatch):
    """Microsoft throttles one thread; the others hold off until its Retry-After instead of piling on."""
    monkeypatch.setitem(LIMITS, "signin_activity", Limit("signin_activity", 100, 1, "app", "test"))
    paused = threading.Event()

    class Watched(Limiter):
        def pause(self, keys, seconds):
            super().pause(keys, seconds)
            paused.set()

    limiter = Watched()
    lock = threading.Lock()
    seen = []

    def answer(req):
        with lock:
            seen.append(time.monotonic())
            if len(seen) == 1:
                return 429, {"Retry-After": "1"}, '{"error": {"code": "TooManyRequests"}}'
        return 200, {"x-ms-throttle-limit-percentage": "0.9"}, '{"value": []}'

    clients = []
    with responses.RequestsMock() as rsps:
        rsps.add_callback(responses.GET, USERS, callback=answer)

        def collect(i):
            graph = GraphClient(lambda: "t", limiter=limiter, app="app", tenant=f"tenant{i}", jitter=lambda: 0.0)
            clients.append(graph)
            if i:
                assert paused.wait(5)  # the others start while tenant 0 is being throttled
            for _ in range(3):
                graph.get("/users", ACTIVITY)

        _threads(4, collect)
    throttled_at, later = seen[0], seen[1:]
    assert len(later) == 12  # 12 requests, and tenant 0's retry
    # Nobody asked again until Microsoft's Retry-After had passed.
    assert min(later) - throttled_at >= 0.95
    stats = [s for c in clients for s in c.telemetry.endpoints.values()]
    assert sum(s.calls for s in stats) == 13 and sum(s.throttled for s in stats) == 1
    assert sum(s.retry_wait for s in stats) == pytest.approx(1.0)
    assert sum(s.limiter_wait for s in stats) >= 3 * 0.9  # the three other threads waited in the limiter
    assert max(s.throttle_pct for s in stats) == pytest.approx(0.9)


def test_a_tenants_429_doesnt_hold_other_tenants(monkeypatch):
    """x-ms-throttle-scope says the limit was one tenant's: other tenants carry on."""
    limiter = Limiter()
    first = threading.Event()
    seen = {}

    def answer(req):
        tenant = req.headers["Authorization"].split()[-1]
        seen.setdefault(tenant, []).append(time.monotonic())
        if tenant == "t0" and len(seen[tenant]) == 1:
            first.set()
            return 429, {"Retry-After": "1", "x-ms-throttle-scope": "Tenant_Application/ReadWrite/app/t0"}, "{}"
        return 200, {}, '{"value": []}'

    with responses.RequestsMock() as rsps:
        rsps.add_callback(responses.GET, SIGN_INS, callback=answer)

        def collect(i):
            graph = GraphClient(lambda: f"t{i}", limiter=limiter, app="app", tenant=f"t{i}", jitter=lambda: 0.0)
            if i:
                first.wait(5)
            graph.get("/auditLogs/signIns")

        _threads(2, collect)
    assert seen["t1"][0] - seen["t0"][0] < 0.5  # tenant 1 wasn't held for tenant 0's second
    assert seen["t0"][1] - seen["t0"][0] >= 0.95


def test_exchange_429_pauses_the_tenant():
    t = FakeTime()
    limiter = Limiter(t.clock, t.sleep)
    with responses.RequestsMock() as rsps:
        url = "https://outlook.office365.com/adminapi/beta/t1/InvokeCommand"
        rsps.post(url, status=429, headers={"Retry-After": "7"})
        rsps.post(url, json={"value": [{"Name": "a"}]})
        exo = ExchangeClient("t1", lambda: "t", limiter=limiter, app="app", sleep=lambda s: None)
        assert exo.invoke("Get-Mailbox") == [{"Name": "a"}]
        assert rsps.calls[0].request.headers["User-Agent"] == USER_AGENT
    # The tenant's Exchange bucket was paused: the retry (like any other thread's call) waited the 7 s there.
    assert t.slept == [pytest.approx(7)]
    stats = exo.telemetry.endpoints["Exchange Get-Mailbox"]
    assert (stats.calls, stats.throttled, stats.retry_wait, stats.limit) == (2, 1, 7.0, "exchange")


# -- What reaches Microsoft ------------------------------------------------------------------


@responses.activate
def test_requests_are_decorated_and_counted():
    responses.get(f"{GRAPH_ROOT}/v1.0/users/abc/drive", json={"id": "d"})
    responses.get(f"{GRAPH_ROOT}/v1.0/groups/0f6c1d6e-4b5c-4f3e-9d2a-0a1b2c3d4e5f/somethingNew", json={})
    responses.get(f"{GRAPH_ROOT}/v1.0/users/abc/drive", status=503)
    graph = GraphClient(lambda: "t", sleep=lambda s: None, max_retries=0)
    graph.get("/users/abc/drive")
    graph.get("/groups/0f6c1d6e-4b5c-4f3e-9d2a-0a1b2c3d4e5f/somethingNew")
    with pytest.raises(GraphError):
        graph.get("/users/abc/drive")
    assert responses.calls[0].request.headers["User-Agent"] == USER_AGENT
    assert USER_AGENT.startswith("ISV|OfficeSentry|OfficeSentry/")
    drive = graph.telemetry.endpoints["v1.0 /users/{id}/drive"]
    assert (drive.calls, drive.errors, drive.units, drive.limit) == (2, 1, 2, "sharepoint")
    assert "v1.0 /groups/{id}/somethingNew" in graph.telemetry.endpoints  # not catalogued: IDs still blanked
    assert graph.telemetry.totals() == {"requests": 3, "throttled": 0, "wait_seconds": 0}


def test_users_ask_for_microsofts_largest_page_with_sign_in_activity(db, two_tenants):
    alpha, _ = two_tenants
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.get(USERS, json={"value": []})
        store.enqueue_run(db, alpha, "users")
        execute_run(db, store.claim_next_run(db), lambda t, c: GraphClient(lambda: "t", sleep=lambda s: None))
        query = urlsplit(rsps.calls[0].request.url).query
    assert "signInActivity" in query and "%24top=500" in query


# -- Stored with the run and shown on its page ------------------------------------------------


def test_every_run_records_its_requests(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    runs = db.execute("SELECT * FROM runs WHERE tenant_id = ?", (alpha,)).fetchall()
    assert {r["collector"] for r in runs} == set(REGISTRY)
    users = next(r for r in runs if r["collector"] == "users")
    assert (users["requests"], users["throttled"], users["wait_seconds"]) == (1, 0, 0)
    rows = db.execute("SELECT * FROM run_requests WHERE run_id = ?", (users["id"],)).fetchall()
    assert [(r["endpoint"], r["throttle_limit"], r["calls"], r["units"]) for r in rows] == [
        ("v1.0 /users (signInActivity)", "signin_activity", 1, 2)
    ]
    assert (
        db.execute(
            "SELECT requests FROM runs WHERE collector = 'domain_health' AND tenant_id = ?", (alpha,)
        ).fetchone()[0]
        == 0
    )  # DNS only: nothing asked of Microsoft
    # Every Microsoft call any collector made is in the endpoint catalogue.
    catalogued = {e.key(v) for e in endpoints.GRAPH for v in e.versions} | {
        e.key() for e in endpoints.EXCHANGE + endpoints.SHAREPOINT
    }
    made = {r[0] for r in db.execute("SELECT DISTINCT endpoint FROM run_requests")}
    assert made - catalogued == set()


def test_recorded_live_requests_are_catalogued():
    """Every request in the live tenant recording matches a catalogue entry (so it is paced and named)."""
    fixture = replay.load()
    missing = set()
    for entry in fixture["entries"]:
        if entry["api"] == "graph" and endpoints.graph(entry["url"]) is None:
            missing.add(endpoints.graph_key(entry["url"]))
        elif entry["api"] == "exchange" and endpoints.exchange(entry["cmdlet"]["CmdletName"]) is None:
            missing.add(entry["cmdlet"]["CmdletName"])
    assert missing == set()


def test_failed_runs_keep_their_telemetry(db, two_tenants):
    alpha, _ = two_tenants
    with responses.RequestsMock() as rsps:
        rsps.get(USERS, status=500, json={"error": {"code": "boom", "message": "down"}})
        store.enqueue_run(db, alpha, "users")
        graph = lambda t, c: GraphClient(lambda: "t", sleep=lambda s: None, max_retries=2)  # noqa: E731
        assert execute_run(db, store.claim_next_run(db), graph) == "failed"
    run = db.execute("SELECT * FROM runs WHERE tenant_id = ? AND collector = 'users'", (alpha,)).fetchone()
    assert run["requests"] == 3
    assert db.execute("SELECT errors FROM run_requests WHERE run_id = ?", (run["id"],)).fetchone()[0] == 3


def test_run_page_shows_requests(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    run = db.execute("SELECT id FROM runs WHERE tenant_id = ? AND collector = 'sign_ins'", (alpha,)).fetchone()
    db.execute(
        "UPDATE run_requests SET throttled = 2, retry_wait = 95, throttle_pct = 0.85 WHERE run_id = ?", (run["id"],)
    )
    db.execute("UPDATE runs SET throttled = 2, wait_seconds = 95 WHERE id = ?", (run["id"],))
    make_user(db, "ann", "admin", all_tenants=True)
    login(client, "ann")
    page = client.get(f"/runs/{run['id']}").get_data(as_text=True)
    assert "Requests to Microsoft" in page and "Requests by endpoint" in page
    assert "/auditLogs/signIns" in page and "5 requests per 10 seconds, per tenant" in page
    assert "2 min" in page and "85%" in page
    old = db.execute("SELECT id FROM runs WHERE tenant_id = ? AND collector = 'users'", (alpha,)).fetchone()["id"]
    db.execute("UPDATE runs SET requests = NULL WHERE id = ?", (old,))  # from before telemetry existed
    page = client.get(f"/runs/{old}").get_data(as_text=True)
    assert "Requests to Microsoft" not in page


def test_request_rows_are_pruned_with_the_run_log(db, two_tenants):
    from datetime import UTC, datetime, timedelta

    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    later = datetime.now(UTC) + timedelta(days=store.RUN_LOG_DAYS + 1)
    assert db.execute("SELECT COUNT(*) FROM run_requests").fetchone()[0] > 0
    store.prune_run_logs(db, now=later)
    assert db.execute("SELECT COUNT(*) FROM run_requests").fetchone()[0] == 0
    assert (
        db.execute("SELECT COUNT(*) FROM runs WHERE requests IS NULL AND tenant_id = ?", (alpha,)).fetchone()[0] == 0
    )  # the totals stay on the run


def test_telemetry_totals_round_waits():
    t = Telemetry()
    t.request("x", None, status=429, retry_wait=1.23456)
    t.request("x", None, status=200, limiter_wait=0.5)
    assert t.totals() == {"requests": 2, "throttled": 1, "wait_seconds": 1.735}
