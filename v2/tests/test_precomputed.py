"""Precomputed cross-tenant rows: What changed and the check matrix read each tenant's stored row
(``reports/precomputed.py``), which must say exactly what the live build says, follow new data, and
never show anyone a tenant they can't open."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from officesentry import findings, lifecycle, store, tasks, tenant_groups, worker
from officesentry.reports import month_changes, precomputed, rollups
from tests.conftest import login, make_user
from tests.seed import seed_tenant
from tests.test_collection_performance import _plan
from tests.test_collectors import USERS
from tests.test_rollups import _backdate

NEWCOMER = {
    "id": "u9",
    "displayName": "Visitor",
    "userPrincipalName": "v#EXT#@y.com",
    "mail": "v@y.com",
    "userType": "Guest",
    "accountEnabled": True,
    "assignedLicenses": [],
    "createdDateTime": "2026-09-20T00:00:00Z",
}


@pytest.fixture
def history(db, two_tenants):
    """Alpha with a collection 40 days ago and one now (an account gone, a guest added, an accepted
    risk); Bravo collected only now."""
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    _backdate(db, alpha, 40)
    db.execute(
        "UPDATE findings SET first_seen = ?", ((datetime.now(UTC) - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ"),)
    )
    seed_tenant(db, alpha, users=[u for u in USERS if u["id"] != "u3"] + [NEWCOMER], precompute=True)
    seed_tenant(db, bravo, precompute=True)
    target = db.execute(
        "SELECT id FROM findings WHERE tenant_id = ? AND resolved_at IS NULL ORDER BY id LIMIT 1", (alpha,)
    ).fetchone()
    findings.accept(db, target["id"], "admin", "known", None)
    return alpha, bravo


def _tenants(db):
    return db.execute("SELECT * FROM tenants ORDER BY id").fetchall()


def _summaries(summaries) -> list:
    return [
        (
            s.tenant["id"],
            s.changes,
            s.new_findings,
            s.new_severe,
            s.fixed,
            s.has_baseline,
            s.areas,
            s.error,
            {k: (f.before, f.after, f.delta) for k, f in s.figures.items()},
        )
        for s in summaries
    ]


def _stored(db, kind: str) -> dict[int, dict]:
    return {
        r["tenant_id"]: json.loads(r["data"])
        for r in db.execute("SELECT tenant_id, data FROM summary_cache WHERE kind = ?", (kind,))
    }


# -- Parity -------------------------------------------------------------------------------------


@pytest.mark.parametrize("days", month_changes.PERIODS)
def test_stored_change_rows_match_the_live_build(db, history, days):
    first = month_changes.across(db, _tenants(db), days)  # builds and stores each row
    assert set(_stored(db, f"changes:{days}")) == set(history)
    stored = month_changes.across(db, _tenants(db), days)
    live = month_changes.across(db, _tenants(db), days, now=datetime.now(UTC))
    assert _summaries(first) == _summaries(stored) == _summaries(live)
    alpha = next(s for s in stored if s.tenant["id"] == history[0])
    if days < 40:  # there's something to compare
        assert alpha.has_baseline and alpha.changes and "Accounts" in alpha.areas
    else:
        assert not alpha.has_baseline
    assert month_changes.across_table(stored, days).rows == month_changes.across_table(live, days).rows


def test_stored_check_rows_match_the_live_build(db, history):
    first = rollups.check_matrix(db, _tenants(db))
    assert set(_stored(db, "checks")) == set(history)
    stored = rollups.check_matrix(db, _tenants(db))
    live = rollups.check_matrix(db, _tenants(db), now=datetime.now(UTC))
    assert first.to_table().rows == stored.to_table().rows == live.to_table().rows
    assert any(r.count("accepted") for r in stored.rows) and any(r.count("fail") for r in stored.rows)


def test_pages_read_the_stored_rows(client, db, history):
    alpha, _ = history
    make_user(db, "admin")
    login(client, "admin")
    client.get("/changes")
    client.get("/across/checks")
    db.execute(
        "UPDATE summary_cache SET data = json_set(data, '$.areas', json_array('Stored area'))"
        " WHERE kind = 'changes:30' AND tenant_id = ?",
        (alpha,),
    )
    some_check = next(iter(_stored(db, "checks")[alpha]["cells"]))
    db.execute(
        "UPDATE summary_cache SET data = json_set(data, ?, json_array('fail', 'Stored summary', 'high', 7))"
        " WHERE kind = 'checks' AND tenant_id = ?",
        (f"$.cells.{some_check}", alpha),
    )
    assert "Stored area" in client.get("/changes").get_data(as_text=True)
    assert "Stored area" in client.get("/changes.csv").get_data(as_text=True)
    assert "Stored summary" in client.get("/across/checks").get_data(as_text=True)
    assert "Fail (high, 7)" in client.get("/across/checks.csv").get_data(as_text=True)


# -- Following the data -------------------------------------------------------------------------


def _change_count(db, tenant_id: int, days: int = 30) -> int:
    return next(s.changes for s in month_changes.across(db, _tenants(db), days) if s.tenant["id"] == tenant_id)


def test_rows_follow_new_collections_accepted_risks_and_findings(db, history):
    alpha, bravo = history
    month_changes.across(db, _tenants(db), 30)
    before = _change_count(db, alpha)
    seed_tenant(db, alpha, users=USERS + [NEWCOMER])  # u3 is back
    live = month_changes.across(db, _tenants(db), 30, now=datetime.now(UTC))
    assert _change_count(db, alpha) == next(s.changes for s in live if s.tenant["id"] == alpha) != before

    def cell(key):
        row = next(r for r in rollups.check_matrix(db, _tenants(db)).rows if r.tenant["id"] == bravo)
        return row.cells[key]

    open_ = db.execute(
        "SELECT * FROM findings WHERE tenant_id = ? AND resolved_at IS NULL ORDER BY id LIMIT 1", (bravo,)
    ).fetchone()
    others = db.execute(
        "SELECT COUNT(*) FROM findings WHERE tenant_id = ? AND resolved_at IS NULL AND rule_key = ?",
        (bravo, open_["rule_key"]),
    ).fetchone()[0]
    assert cell(open_["rule_key"]).status == "fail"
    for f in db.execute(
        "SELECT id FROM findings WHERE tenant_id = ? AND rule_key = ? AND resolved_at IS NULL",
        (bravo, open_["rule_key"]),
    ).fetchall():
        findings.accept(db, f["id"], "admin", "known", None)
    assert cell(open_["rule_key"]).status == "accepted", others
    findings.unaccept(db, open_["id"])
    assert cell(open_["rule_key"]).status == "fail"
    # An acceptance that runs out counts as of now, without anything else changing.
    past = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute(
        "UPDATE findings SET accepted_until = ? WHERE tenant_id = ? AND rule_key = ?", (past, bravo, open_["rule_key"])
    )
    assert cell(open_["rule_key"]).status == "fail"


def test_a_row_built_yesterday_isnt_used_today(db, history):
    alpha, _ = history
    month_changes.across(db, _tenants(db), 30)
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    row = db.execute(
        "SELECT fingerprint FROM summary_cache WHERE tenant_id = ? AND kind = 'changes:30'", (alpha,)
    ).fetchone()
    parts = json.loads(row["fingerprint"])
    parts[1] = yesterday
    db.execute(
        "UPDATE summary_cache SET fingerprint = ?, data = json_set(data, '$.changes', 4242)"
        " WHERE tenant_id = ? AND kind = 'changes:30'",
        (json.dumps(parts), alpha),
    )
    assert _change_count(db, alpha) != 4242


def test_the_start_of_the_period_is_part_of_the_row(db, two_tenants):
    """A collection moving out past the start of the period changes what the period starts from,
    though the tenant's latest run is the same."""
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    _backdate(db, alpha, 25)
    first = [r["id"] for r in db.execute("SELECT id FROM runs WHERE tenant_id = ?", (alpha,))]
    seed_tenant(db, alpha, users=[u for u in USERS if u["id"] != "u3"])
    assert not month_changes.across(db, _tenants(db), 30)[0].has_baseline  # stored: nothing 30 days old
    old = (datetime.now(UTC) - timedelta(days=35)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute(f"UPDATE runs SET finished_at = ? WHERE id IN ({','.join('?' * len(first))})", (old, *first))
    stored = month_changes.across(db, _tenants(db), 30)
    assert _summaries(stored) == _summaries(month_changes.across(db, _tenants(db), 30, now=datetime.now(UTC)))
    assert stored[0].has_baseline and stored[0].changes


def test_rebuild_ignores_rows_that_look_current(db, history):
    alpha, _ = history
    precomputed.refresh(db, store.get_tenant(db, alpha))
    db.execute(
        "UPDATE summary_cache SET data = json_set(data, '$.changes', 4242) WHERE tenant_id = ?"
        " AND kind LIKE 'changes:%'",
        (alpha,),
    )
    precomputed.refresh(db, store.get_tenant(db, alpha))
    assert _stored(db, "changes:30")[alpha]["changes"] == 4242  # still matches: left alone
    precomputed.refresh(db, store.get_tenant(db, alpha), rebuild=True)
    assert _stored(db, "changes:30")[alpha]["changes"] != 4242


# -- Worker ----------------------------------------------------------------------------------------


def test_a_tenants_collection_queues_its_rows(cfg, db, history):
    alpha, bravo = history
    queued = db.execute("SELECT key, priority FROM tasks WHERE kind = 'precompute' AND status = 'queued'").fetchall()
    assert {r["key"] for r in queued} == {f"precompute:{alpha}", f"precompute:{bravo}"}  # once each
    assert all(r["priority"] < tasks.PRIORITY_BACKGROUND for r in queued)
    tasks.drain(None, db, None, cfg)
    for kind in ("changes:7", "changes:30", "changes:90", "checks"):
        assert set(_stored(db, kind)) == {alpha, bravo}, kind


def test_the_daily_pass_queues_every_tenant_and_rebuilds_after_pruning(cfg, db, history, monkeypatch):
    alpha, bravo = history
    archived = store.create_tenant(db, "Gone Ltd", "gone.onmicrosoft.com", None)
    lifecycle.archive(db, store.get_tenant(db, archived), "admin")
    db.execute("DELETE FROM tasks")
    monkeypatch.setattr(worker, "prune", lambda conn, cfg: 0)
    worker._daily(db, cfg)
    rows = db.execute("SELECT tenant_id, payload FROM tasks WHERE kind = 'precompute'").fetchall()
    assert {r["tenant_id"] for r in rows} == {alpha, bravo}
    assert all(not json.loads(r["payload"]).get("rebuild") for r in rows)
    monkeypatch.setattr(worker, "prune", lambda conn, cfg: 3)
    worker._daily(db, cfg)  # the queued tasks are reused, now rebuilding
    rows = db.execute("SELECT payload FROM tasks WHERE kind = 'precompute'").fetchall()
    assert len(rows) == 2 and all(json.loads(r["payload"])["rebuild"] for r in rows)


def test_new_lookups_use_an_index(db, two_tenants):
    db.execute("ANALYZE")
    for sql, args in [
        (
            "SELECT finished_at, id FROM runs WHERE tenant_id = ? AND finished_at IS NOT NULL AND finished_at <= ?"
            " ORDER BY finished_at DESC, id DESC LIMIT 1",
            (1, "2026-01-01T00:00:00Z"),
        ),
        ("SELECT COUNT(*) FROM findings WHERE tenant_id = ? AND resolved_at IS NULL", (1,)),
        (
            "SELECT tenant_id, fingerprint, data FROM summary_cache WHERE kind = ? AND tenant_id IN (?, ?)",
            ("checks", 1, 2),
        ),
    ]:
        plan = _plan(db, sql, *args)
        assert "SCAN runs" not in plan and "SCAN findings" not in plan and "SCAN summary_cache" not in plan, sql


def test_deleting_a_tenant_deletes_its_rows(db, history):
    alpha, _ = history
    month_changes.across(db, _tenants(db), 30)
    lifecycle.delete_tenant(db, store.get_tenant(db, alpha), "admin")
    assert alpha not in _stored(db, "changes:30")


# -- Who sees what -----------------------------------------------------------------------------------


def test_stored_rows_only_reach_people_who_see_the_tenant(client, db, history):
    alpha, bravo = history
    make_user(db, "admin")
    login(client, "admin")
    for path in ("/changes", "/across/checks"):  # stores both tenants' rows
        page = client.get(path).get_data(as_text=True)
        assert "Alpha Ltd" in page and "Bravo Inc" in page
    client.post("/logout")

    make_user(db, "ann", role="analyst", tenants=[alpha])  # granted one tenant
    gold = tenant_groups.create(db, {"name": "Gold", "category": "", "description": ""}, "test")
    tenant_groups.set_members(db, gold, [bravo])
    ben = make_user(db, "ben", role="analyst")  # granted a group
    tenant_groups.set_user_groups(db, ben, [gold])
    for user, sees, hidden in (("ann", "Alpha Ltd", "Bravo Inc"), ("ben", "Bravo Inc", "Alpha Ltd")):
        login(client, user)
        for path in ("/changes", "/changes.csv", "/changes?days=7", "/across/checks", "/across/checks.csv"):
            page = client.get(path).get_data(as_text=True)
            assert sees in page and hidden not in page, (user, path)
        client.post("/logout")

    login(client, "admin")
    client.get(f"/?group={gold}")  # the group picker narrows the stored rows too
    for path in ("/changes", "/across/checks"):
        page = client.get(path).get_data(as_text=True)
        assert "Bravo Inc" in page and "Alpha Ltd" not in page, path
