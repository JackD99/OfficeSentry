"""Activity history (events.py): incremental collection that survives crashes, timeouts and overlapping runs,
daily summaries, retention, tenant deletion and isolation, backups, and the Activity history page."""

import csv
import io
import json
import os
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import responses

from officesentry import backups, changes, events, lifecycle, retention, store, tasks
from officesentry.graph import GRAPH_ROOT, GraphClient
from officesentry.reports import activity_history
from tests import adminactivitydata as ad
from tests import graphdata as gd
from tests.conftest import login, make_user

BETA = f"{GRAPH_ROOT}/beta"
V1 = f"{GRAPH_ROOT}/v1.0"
NOW = datetime(2026, 10, 9, 17, 0, tzinfo=UTC)


class Crash(BaseException):
    """The worker dying mid-run: not an error anything in the collector catches."""


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    clock = SimpleNamespace(now=NOW)
    monkeypatch.setattr(events, "clock", lambda: clock.now)
    return clock


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _signins(count=60, upns=("ann@x.com", "bob@x.com", "cat@x.com"), prefix="si", end=NOW, step_hours=2.5):
    """Sign-ins over the days before ``end``, newest first, every fourth sharing the previous one's time."""
    out, when = [], end - timedelta(minutes=30)
    for n in range(count):
        if n % 4:
            when -= timedelta(hours=step_hours)
        row = gd._signin(
            n,
            _iso(when),
            upns[n % len(upns)],
            f"u{n % len(upns)}",
            [0, 0, 50126, 50074][n % 4],
            ip=f"198.51.100.{n % 5}",
            country=["GB", "FR"][n % 2],
            mfa=bool(n % 3),
        )
        out.append({**row, "id": f"{prefix}-{n}"})
    return out


def _fake_log(
    rows, *, path="/auditLogs/signIns", root=BETA, field="createdDateTime", page=7, fail=lambda flt, skip, n: None
):
    """A fake log honouring ``ge``/``le`` filters, newest first, in pages. ``fail(filter, skip, call)`` may
    return an HTTP status to answer instead, or raise to simulate a crash."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        query = parse_qs(urlsplit(request.url).query)
        flt, skip = query["$filter"][0], int(query.get("$skiptoken", ["0"])[0])
        status = fail(flt, skip, calls["n"])
        if status:
            return status, {}, json.dumps({"error": {"code": "GatewayTimeout", "message": "Request timed out"}})
        clauses = [c.split(" ") for c in flt.split(" and ")]
        ops = {"ge": str.__ge__, "le": str.__le__}
        match = sorted(
            (r for r in rows if all(ops[op](r[field], v) for _, op, v in clauses)), key=lambda r: r[field], reverse=True
        )
        body = {"value": match[skip : skip + page]}
        if skip + page < len(match):
            body["@odata.nextLink"] = f"{root}{path}?$filter={flt}&$skiptoken={skip + page}"
        return 200, {}, json.dumps(body)

    responses.add_callback(responses.GET, f"{root}{path}", callback=handler, content_type="application/json")
    return calls


def _ctx(db, tenant_id):
    lines = []
    return SimpleNamespace(
        conn=db,
        tenant={"id": tenant_id},
        log=lines.append,
        lines=lines,
        graph=GraphClient(lambda: "t", sleep=lambda s: None),
    )


def _ingest(db, tenant_id, budget=100, **kwargs):
    return events.ingest(_ctx(db, tenant_id), events.SIGNIN_LOG, 30, budget, **kwargs)


def _stored(db, tenant_id, start=NOW - timedelta(days=40), end=NOW + timedelta(days=1)):
    return [s["id"] for s in events.signins_between(db, tenant_id, start, end)]


def _daily(db, tenant_id):
    return {
        (r.day, r.user, r.app, r.country): (r.succeeded, r.failed, r.interrupted, r.mfa, r.single_factor)
        for r in events.daily_between(db, tenant_id, "2026-08-01", "2026-12-31")
    }


def _expected_daily(rows):
    from officesentry import signins

    out = {}
    for s in rows:
        k = (s["createdDateTime"][:10], s["userPrincipalName"], s["appDisplayName"], s["location"]["countryOrRegion"])
        ok, failed, interrupted, mfa, single = out.get(k, (0, 0, 0, 0, 0))
        result = signins.outcome(s["status"]["errorCode"])
        out[k] = (
            ok + (result == "success"),
            failed + (result == "failure"),
            interrupted + (result == "interrupted"),
            mfa + (result == "success" and signins.mfa_used(s) is True),
            single + (result == "success" and signins.mfa_used(s) is False),
        )
    return out


# -- Collection ------------------------------------------------------------------------------------


@responses.activate
def test_a_crash_mid_collection_loses_nothing_and_resumes(db, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    rows = _signins()

    def crash(flt, skip, n):
        if n == 4:
            raise Crash

    _fake_log(rows, fail=crash)
    with pytest.raises(Crash):
        _ingest(db, alpha)
    # Three pages were stored and the cursor says where to carry on: the rest of the month is still a gap.
    stored = _stored(db, alpha)
    assert len(stored) == 21 and set(stored) == {r["id"] for r in rows[:21]}
    cov = events.coverage(db, alpha, "signins")
    assert cov.gaps and cov.gaps[0][1] == events._epoch(rows[20]["createdDateTime"])
    assert not cov.up_to_date and not cov.pending
    assert _daily(db, alpha) == _expected_daily(rows[:21])  # what was read is already summarised

    # The worker also dies after a page is written but before the cursor is saved: it reads that page again.
    responses.reset()
    _fake_log(rows)
    saves = {"n": 0}
    real = events.save_coverage

    def dies(conn, tenant_id, cov):
        saves["n"] += 1
        if saves["n"] == 4:
            raise Crash
        real(conn, tenant_id, cov)

    monkeypatch.setattr(events, "save_coverage", dies)
    with pytest.raises(Crash):
        _ingest(db, alpha)
    monkeypatch.setattr(events, "save_coverage", real)

    got = _ingest(db, alpha)
    stored = _stored(db, alpha)
    assert sorted(stored) == sorted(r["id"] for r in rows)  # every sign-in, once
    assert got.coverage.up_to_date and not got.coverage.pending
    assert _daily(db, alpha) == _expected_daily([r for r in rows if r["isInteractive"]])


@responses.activate
def test_overlapping_runs_store_each_event_once_and_catch_late_arrivals(db, two_tenants, _clock):
    alpha, _ = two_tenants
    rows = _signins(30)
    log = list(rows)
    _fake_log(log)
    first = _ingest(db, alpha)
    assert first.new == len(rows) and first.coverage.up_to_date

    # Two hours later: new sign-ins, and one Microsoft only now added from before the first run read up to.
    _clock.now = NOW + timedelta(hours=2)
    newer = _signins(5, prefix="new", end=_clock.now, step_hours=0.3)
    late = {**gd._signin(999, _iso(NOW - timedelta(hours=1)), "dan@x.com", "u9"), "id": "late-1"}
    log += [*newer, late]
    second = _ingest(db, alpha)
    first_filter = parse_qs(urlsplit(responses.calls[-1].request.url).query)["$filter"][0]
    assert first_filter == f"createdDateTime ge {_iso(NOW - timedelta(hours=6))}"  # re-reads the last six hours
    assert second.new == len(newer) + 1 and second.read > second.new  # the overlap's rows were seen again
    assert sorted(_stored(db, alpha, end=_clock.now)) == sorted(r["id"] for r in log)
    assert _daily(db, alpha) == _expected_daily(log)

    # The same run again changes nothing.
    assert _ingest(db, alpha).new == 0
    assert len(_stored(db, alpha, end=_clock.now)) == len(log)


@responses.activate
def test_a_page_budget_stops_the_run_and_the_next_carries_on(db, two_tenants):
    alpha, _ = two_tenants
    rows = _signins(50)
    _fake_log(rows)
    got = _ingest(db, alpha, budget=2)
    assert got.budget_hit and got.pages == 2 and len(_stored(db, alpha)) == 14
    # Newest first, so what's missing is the oldest part, and the coverage says exactly which.
    missing = got.coverage.missing(int((NOW - timedelta(days=30)).timestamp()), int(NOW.timestamp()))
    assert missing and missing[-1][1] == events._epoch(rows[13]["createdDateTime"])
    while not got.coverage.up_to_date:
        got = _ingest(db, alpha, budget=2)
    assert sorted(_stored(db, alpha)) == sorted(r["id"] for r in rows)


@responses.activate
def test_after_the_ten_minute_give_up_the_next_run_resumes_from_the_cursor(db, two_tenants):
    alpha, _ = two_tenants
    rows = _signins(40)
    # The month-long query and every day after the first slice time out; the deadline has passed by then.
    slow = {"on": True}
    _fake_log(rows, fail=lambda flt, skip, n: 504 if slow["on"] and n > 1 else None)
    ticks = iter([False, True])
    got = _ingest(db, alpha, retries=0, deadline=lambda: lambda: next(ticks, True))
    assert got.gave_up and got.failures and not got.coverage.up_to_date
    assert len(_stored(db, alpha)) == 7  # the first page, before Microsoft stopped answering

    slow["on"] = False
    got = _ingest(db, alpha, retries=0)
    assert got.coverage.up_to_date and sorted(_stored(db, alpha)) == sorted(r["id"] for r in rows)


@responses.activate
def test_a_log_that_never_answers_raises_after_saving_the_cursor(db, two_tenants):
    alpha, _ = two_tenants
    _fake_log([], fail=lambda flt, skip, n: 504)
    with pytest.raises(Exception, match="timed out"):
        _ingest(db, alpha, retries=0, deadline=lambda: lambda: True)
    cov = events.coverage(db, alpha, "signins")
    assert cov.gaps and cov.since is not None


def test_ranges_microsoft_no_longer_keeps_are_lost_or_move_the_start():
    day = events.DAY
    now = 100 * day
    floor = now - 30 * day
    # Never read at the bottom: the history just starts later. In the middle: lost, and remembered.
    cov = events.Coverage(
        "signins",
        since=floor - 10 * day,
        read_to=now - day,
        gaps=[[floor - 3 * day, floor - day], [floor - 10 * day, floor - 8 * day]],
    )
    events.extend(cov, now, floor)
    assert cov.since == floor - 8 * day
    assert cov.lost == [[floor - 3 * day, floor - day]]
    assert cov.gaps == [[now - day - events.OVERLAP, now]]
    assert cov.missing(floor - 5 * day, now) == [(floor - 3 * day, floor - day), (now - day - events.OVERLAP, now)]


@responses.activate
def test_directory_audits_are_kept_compressed_and_read_back_as_microsoft_sent_them(db, two_tenants):
    alpha, _ = two_tenants
    audits = [
        {**ad.GLOBAL_ADMIN_GRANT, "activityDateTime": _iso(NOW - timedelta(days=2))},
        {**ad.CA_DISABLED, "activityDateTime": _iso(NOW - timedelta(days=1))},
    ]
    _fake_log(audits, path="/auditLogs/directoryAudits", root=V1, field="activityDateTime")
    got = events.ingest(_ctx(db, alpha), events.AUDIT_LOG, 30, 10)
    assert got.new == 2
    back = list(events.audits_between(db, alpha, NOW - timedelta(days=30), NOW))
    assert back == sorted(audits, key=lambda e: e["activityDateTime"], reverse=True)

    # The What changed feed lists them with their own dates.
    found = changes.audit_changes(db, alpha, _iso(NOW - timedelta(days=7)))
    assert [c.subject for c in found] and all("on 2026-10-0" in c.detail for c in found)


# -- Isolation, deletion, retention, backups ---------------------------------------------------


@responses.activate
def test_tenants_never_see_each_others_events_and_deletion_is_clean(db, two_tenants):
    alpha, bravo = two_tenants
    a_rows, b_rows = _signins(20, prefix="a"), _signins(15, upns=("zed@y.com",), prefix="b")
    _fake_log(a_rows)
    _ingest(db, alpha)
    responses.reset()
    _fake_log(b_rows)
    _ingest(db, bravo)
    assert sorted(_stored(db, alpha)) == sorted(r["id"] for r in a_rows)
    assert sorted(_stored(db, bravo)) == sorted(r["id"] for r in b_rows)
    assert {r.user for r in events.daily_between(db, bravo, "2026-08-01", "2026-12-31")} == {"zed@y.com"}
    assert events.daily_for(db, alpha, ["zed@y.com"], "2026-08-01", "2026-12-31") == []
    alpha_key = events.tenant_key(db, alpha)

    removed = lifecycle.delete_tenant(db, store.get_tenant(db, alpha), "test")
    assert removed["history_signins"] == len(a_rows)
    # The files are cleaned by the erase task the delete queues, in the background.
    assert tasks.drain(None, db, None, None) == 1
    assert events.counts(db, bravo)["signins"] == len(b_rows)
    on_disk = b"".join(p.read_bytes() for p in events.root(db).rglob("*.db*"))
    assert b"ann@x.com" not in on_disk and b"zed@y.com" in on_disk
    for path in (events.root(db)).rglob("*.db"):
        with closing(sqlite3.connect(path)) as raw:  # `with` on a connection alone commits, never closes
            table = events.TABLE[path.parent.name]
            assert not raw.execute(f"SELECT 1 FROM {table} WHERE tenant = ?", (alpha_key,)).fetchone()
            assert not raw.execute("SELECT 1 FROM label WHERE value LIKE 'ann@%'").fetchone()

    # A new tenant never gets the deleted one's key, so leftovers could never show under it.
    gamma = store.create_tenant(
        db,
        "Gamma",
        "gamma.onmicrosoft.com",
        db.execute("SELECT connection_id FROM tenants WHERE id = ?", (bravo,)).fetchone()[0],
    )
    assert events.tenant_key(db, gamma, create=True) > max(alpha_key, events.tenant_key(db, bravo))


@responses.activate
def test_the_daily_sweep_removes_rows_a_failed_delete_left_behind(db, two_tenants):
    alpha, bravo = two_tenants
    _fake_log(_signins(10))
    _ingest(db, alpha)
    _ingest(db, bravo)
    db.execute("DELETE FROM event_tenants WHERE tenant_id = ?", (alpha,))  # as if the files' delete never ran
    assert events.sweep(db) >= 10  # the sign-ins and their daily summaries
    assert events.counts(db, bravo)["signins"] == 10
    assert events.sweep(db) == 0


@responses.activate
def test_retention_deletes_whole_old_months_and_counts_first(db, two_tenants, cfg):
    alpha, _ = two_tenants
    rows = _signins(50, end=NOW - timedelta(days=5), step_hours=24)  # about one a day, back into August
    _fake_log(rows)
    events.ingest(_ctx(db, alpha), events.SIGNIN_LOG, 60, 100)
    assert len(_stored(db, alpha, start=NOW - timedelta(days=90))) == len(rows)
    months = events.months(events.root(db), "signins")
    assert months[0] < "2026-09"

    cutoff = datetime(2026, 9, 15, tzinfo=UTC)
    older = sum(1 for r in rows if r["createdDateTime"] < "2026-09-15")
    assert events.prune(db, "signins", cutoff, dry_run=True) == older
    assert len(_stored(db, alpha, start=NOW - timedelta(days=90))) == len(rows)  # a dry run deletes nothing
    assert events.prune(db, "signins", cutoff) == older
    assert events.months(events.root(db), "signins")[0] == "2026-09"  # August's file is gone
    kept = list(events.signins_between(db, alpha, NOW - timedelta(days=90), NOW))
    assert len(kept) == len(rows) - older and all(r["createdDateTime"] >= "2026-09-15" for r in kept)

    # The defaults follow D18, and the daily pass uses them.
    policy = retention.defaults(cfg)
    assert (policy.history_signin_days, policy.history_daily_months, policy.history_audit_years) == (180, 25, 7)
    counts = retention.prune_history(db, retention.Policy(history_signin_days=20), NOW, dry_run=True)
    edge = _iso(NOW - timedelta(days=20))
    assert counts["history_signin_days"] == sum(1 for r in kept if r["createdDateTime"] < edge) > 0


@responses.activate
def test_backups_copy_the_history_and_a_restore_puts_it_back(db, two_tenants, cfg, tmp_path):
    alpha, _ = two_tenants
    _fake_log(_signins(20))
    _ingest(db, alpha)
    source = events.root(db)
    dest = tmp_path / "backups" / "events"
    copied, _ = backups.mirror_events(source, dest)
    assert copied == len(list(source.rglob("*.db")))
    assert backups.mirror_events(source, dest) == (0, 0)  # unchanged files aren't copied again

    for path in list(source.rglob("*.db")):
        events._remove(path)
    assert backups.restore_events(dest, source) == copied
    assert len(_stored(db, alpha)) == 20


@responses.activate
def test_a_backup_moved_without_its_history_reads_back_what_microsoft_keeps(db, two_tenants, cfg, tmp_path):
    alpha, _ = two_tenants
    _fake_log(_signins(20))
    _ingest(db, alpha)
    saved, _ = backups.backup(cfg.db_path, tmp_path / "old-server", keep=3)
    if os.name != "nt":
        assert oct((tmp_path / "old-server" / "events" / "manifest.json").stat().st_mode & 0o777) == "0o600"
    for path in list(events.root(db).rglob("*.db")):  # the new server's data folder has no history yet
        events._remove(path)

    # With the events folder beside it, the history comes back and the cursor still says how far it was read.
    done = backups.restore(cfg.db_path, saved, worker_running=False)
    assert done["events"] > 0 and done["history_forgotten"] == 0
    assert len(_stored(db, alpha)) == 20 and events.coverage(db, alpha, "signins").read_to is not None

    # The database file alone: the cursor would claim months that aren't there, so it's forgotten, and the
    # next collection reads back as far as Microsoft keeps the log.
    for path in list(events.root(db).rglob("*.db")):
        events._remove(path)
    alone = tmp_path / "moved" / saved.name
    alone.parent.mkdir()
    shutil.copy(saved, alone)
    done = backups.restore(cfg.db_path, alone, worker_running=False)
    assert done["events"] == 0 and done["history_forgotten"] == 1
    assert events.coverage(db, alpha, "signins").read_to is None
    _ingest(db, alpha)
    assert len(_stored(db, alpha)) == 20
    assert not list(cfg.data_dir.glob("restore-*"))  # the unpacked copy is gone, with SQLite's files beside it


# -- The page ---------------------------------------------------------------------------------------


@responses.activate
def test_activity_history_page_leads_with_how_far_back_it_goes(client, db, two_tenants, monkeypatch):
    alpha, bravo = two_tenants
    rows = _signins(30)
    rows[0] = {**rows[0], "userPrincipalName": "=cmd|' /C calc'!A0"}
    _fake_log(rows)
    _ingest(db, alpha)
    make_user(db, "admin")
    login(client, "admin")

    page = client.get(f"/tenants/{alpha}/activity")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Every sign-in" in html and "Sign-ins are detailed from" in html and "Up to date as of" in html
    assert "Sign-ins per day" in html and "Activity history" in html and "180 days" in html
    month = NOW.strftime("%Y-%m")
    assert f"/tenants/{alpha}/activity/sign-ins-{month}.csv" in html

    resp = client.get(f"/tenants/{alpha}/activity/sign-ins-{month}.csv")
    assert resp.status_code == 200
    table = list(csv.reader(io.StringIO(resp.get_data(as_text=True).lstrip("﻿"))))
    assert table[0] == list(activity_history.SIGNIN_CSV_COLUMNS)
    in_month = [r for r in rows if r["createdDateTime"].startswith(month)]
    assert len(table) - 1 == len(in_month) and table[1][0] <= table[-1][0]
    assert all(not row[1].startswith("=") for row in table[1:])  # formulas are neutralised
    assert client.get(f"/tenants/{alpha}/activity/sign-ins-2026-13.csv").status_code == 404

    # Nothing kept yet: an empty state, not an error.
    html = client.get(f"/tenants/{bravo}/activity").get_data(as_text=True)
    assert "No activity history yet" in html


def test_activity_history_is_for_staff_with_access(client, db, two_tenants):
    alpha, bravo = two_tenants
    make_user(db, "someone", role="analyst", tenants=[alpha])
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "someone")
    assert client.get(f"/tenants/{alpha}/activity").status_code == 200
    assert client.get(f"/tenants/{bravo}/activity").status_code == 404
    assert client.get(f"/tenants/{bravo}/activity/sign-ins-2026-10.csv").status_code == 404
    other = client.application.test_client()
    login(other, "client")
    assert other.get(f"/tenants/{alpha}/activity").status_code == 403
