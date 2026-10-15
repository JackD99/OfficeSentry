"""Data retention: the admin's settings, the daily prune and its dry run."""

from datetime import UTC, datetime, timedelta

import pytest

from officesentry import retention, store, worker
from officesentry.collectors import REGISTRY
from officesentry.db import audit

from .conftest import login, make_user
from .seed import seed_tenant


def iso(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_with(db, tenant, collector, days_ago, kinds=("item",)):
    rid = store.enqueue_run(db, tenant, collector)
    store.claim_next_run(db)
    for kind in kinds:
        store.add_objects(db, rid, kind, [("x", {"ip": "203.0.113.9"})])
    store.finish_run(db, rid, "succeeded")
    db.execute("UPDATE runs SET created_at = ?, finished_at = ? WHERE id = ?", (iso(days_ago), iso(days_ago), rid))
    return rid


def kinds_of(db, rid):
    return {r[0] for r in db.execute("SELECT kind FROM objects WHERE run_id = ?", (rid,))}


def finding(db, tenant, key, first_seen, resolved_at=None, accepted_at=None):
    return db.execute(
        "INSERT INTO findings (tenant_id, rule_key, object_id, severity, subject, detail, first_seen, last_seen,"
        " resolved_at, accepted_at) VALUES (?, ?, 'o', 'high', 'someone@alpha', '', ?, ?, ?, ?)",
        (tenant, key, first_seen, first_seen, resolved_at, accepted_at),
    ).lastrowid


def issued(db, tenant, kind, days_ago, filename="r.pdf"):
    return db.execute(
        "INSERT INTO issued_reports (tenant_id, kind, title, period, issued_at, trigger, filename, content_type,"
        " sha256, size, data) VALUES (?, ?, 'Review', '2025-01', ?, 'schedule', ?, 'application/pdf', '',"
        " 1, x'00')",
        (tenant, kind, iso(days_ago), filename),
    ).lastrowid


# -- Settings ---------------------------------------------------------------------------------


def test_defaults_match_todays_behaviour(db, cfg):
    p = retention.load(db, cfg)
    assert (p.keep_daily, p.keep_monthly) == (cfg.keep_daily, cfg.keep_monthly) == (30, 12)
    assert p.run_log_days == store.RUN_LOG_DAYS == 90
    assert p.signin_days is p.findings_days is p.audit_days is p.issued_days is None


def test_default_prune_changes_nothing_new(db, cfg, two_tenants):
    """With no settings, the daily pass deletes only what it always did."""
    alpha, _ = two_tenants
    old_signins = run_with(db, alpha, "sign_ins", 400, ("sign_in_account",))
    latest = run_with(db, alpha, "sign_ins", 2, ("sign_in_account",))
    first = finding(db, alpha, "a", iso(900))
    gone = finding(db, alpha, "b", iso(800), resolved_at=iso(700))
    audit(db, "admin", "user_created", detail="x")
    db.execute("UPDATE audit_log SET ts = ?", (iso(3000),))
    report = issued(db, alpha, "review", 3000)
    issued(db, alpha, "review", 1)
    worker.prune(db, cfg)
    assert kinds_of(db, latest) == {"sign_in_account"}
    assert kinds_of(db, old_signins) == {"sign_in_account"}  # a monthly snapshot: sign-ins stay with it
    assert db.execute("SELECT COUNT(*) FROM findings WHERE id IN (?, ?)", (first, gone)).fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1
    assert db.execute("SELECT 1 FROM issued_reports WHERE id = ?", (report,)).fetchone()


FORM = {
    "keep_daily": "14",
    "keep_monthly": "6",
    "signin_days": "60",
    "findings_days": "180",
    "run_log_days": "30",
    "audit_days": "400",
    "issued_days": "730",
    "history_signin_days": "180",
    "history_daily_months": "25",
    "history_audit_years": "7",
    "history_activity_months": "24",
    "state_months": "25",
}


@pytest.mark.parametrize("role", ["analyst", "client"])
def test_only_admins_see_or_change_retention(client, db, role):
    make_user(db, "someone", role=role)
    login(client, "someone")
    assert client.get("/admin/retention").status_code == 403
    assert client.post("/admin/retention", data=FORM).status_code == 403
    assert not db.execute("SELECT 1 FROM settings WHERE key LIKE 'retention.%'").fetchone()


def test_admin_saves_retention_and_it_is_audited(client, db, cfg):
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/admin/retention")
    assert page.status_code == 200 and b"Sign-in detail" in page.data and b"Never less than 365 days" in page.data
    form = {**FORM, "issued_days_forever": "1"}
    assert client.post("/admin/retention", data=form).status_code == 302
    p = retention.load(db, cfg)
    assert (p.keep_daily, p.keep_monthly, p.signin_days, p.findings_days, p.run_log_days, p.audit_days) == (
        14,
        6,
        60,
        180,
        30,
        400,
    )
    assert p.issued_days is None
    detail = db.execute("SELECT detail FROM audit_log WHERE action = 'retention_updated'").fetchone()[0]
    assert "Daily snapshots: 30 days → 14 days" in detail
    assert "Sign-in detail: as long as its snapshot → 60 days" in detail
    client.post("/admin/retention", data=form)  # no change, no second entry
    assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'retention_updated'").fetchone()[0] == 1


def test_the_page_says_what_the_settings_keep(client, db, two_tenants):
    """How long sign-in detail stays follows the monthly snapshots setting (it said "about 13 months" whatever
    that was), and the dry run's counts are written with thousands separators."""
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    assert "which is about 12 months for monthly snapshots" in client.get("/admin/retention").get_data(as_text=True)
    finding(db, alpha, "first-scan", iso(1000))
    db.executemany(
        "INSERT INTO findings (tenant_id, rule_key, object_id, severity, subject, detail, first_seen, last_seen,"
        " resolved_at) VALUES (?, 'a', ?, 'high', 'someone@alpha', '', ?, ?, ?)",
        [(alpha, f"o{n}", iso(900), iso(900), iso(800)) for n in range(1234)],
    )
    client.post("/admin/retention", data={**FORM, "keep_monthly_forever": "1"})
    page = client.get("/admin/retention").get_data(as_text=True)
    assert "which is forever for monthly snapshots" in page and "13 months" not in page
    assert "about 1,234 resolved findings" in page


@pytest.mark.parametrize(
    "field,value",
    [
        ("audit_days", "364"),
        ("issued_days", "30"),
        ("signin_days", "7"),
        ("keep_daily", "0"),
        ("run_log_days", "soon"),
        ("history_signin_days", "29"),
        ("history_audit_years", "0"),
    ],
)
def test_out_of_range_settings_are_refused(client, db, cfg, field, value):
    make_user(db, "admin")
    login(client, "admin")
    resp = client.post("/admin/retention", data={**FORM, field: value}, follow_redirects=True)
    assert b"must be between" in resp.data or b"whole number" in resp.data
    assert retention.load(db, cfg) == retention.defaults(cfg)


def test_audit_floor_holds_even_when_stored_lower(db, cfg):
    db.execute("INSERT INTO settings (key, value) VALUES ('retention.audit_days', '10')")
    assert retention.load(db, cfg).audit_days == retention.AUDIT_FLOOR_DAYS
    audit(db, "admin", "user_created", detail="recent")
    audit(db, "admin", "user_created", detail="old")
    db.execute("UPDATE audit_log SET ts = ? WHERE detail = 'recent'", (iso(300),))
    db.execute("UPDATE audit_log SET ts = ? WHERE detail = 'old'", (iso(400),))
    assert retention.prune_audit_log(db, 10) == 1  # 10 days is raised to the 365-day floor
    assert [r[0] for r in db.execute("SELECT detail FROM audit_log")] == ["recent"]


# -- Each prune ------------------------------------------------------------------------------


def test_sign_in_detail_is_pruned_after_its_period(db, two_tenants):
    alpha, _ = two_tenants
    old = run_with(db, alpha, "sign_ins", 45, ("sign_in_status", "sign_in_account", "sign_in_failure_ip"))
    recent = run_with(db, alpha, "sign_ins", 10, ("sign_in_account",))
    legacy = run_with(db, alpha, "legacy_signins", 45, ("legacy_signin_status", "legacy_signin"))
    sizes = run_with(db, alpha, "mailbox_usage", 45, ("mailbox_size",))
    users = run_with(db, alpha, "users", 45, ("user",))
    policy = retention.Policy(signin_days=30)
    assert retention.dry_run(db, policy)["signin_days"] == 5
    assert retention.prune_signin_detail(db, 30) == 5
    assert not kinds_of(db, old)
    assert not kinds_of(db, legacy)  # accounts, addresses and countries of legacy sign-ins go too
    assert kinds_of(db, recent) == {"sign_in_account"}
    assert kinds_of(db, sizes) == {"mailbox_size"}
    assert kinds_of(db, users) == {"user"}
    assert retention.prune_signin_detail(db, None) == 0


def test_every_data_source_that_stores_sign_in_detail_is_pruned(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    marks = ",".join("?" * len(retention.SIGNIN_KINDS))
    sources = {
        r[0]
        for r in db.execute(
            f"SELECT DISTINCT r.collector FROM runs r JOIN objects o ON o.run_id = r.id WHERE o.kind IN ({marks})",
            retention.SIGNIN_KINDS,
        )
    }
    assert sources == set(retention.SIGNIN_COLLECTORS)
    assert set(retention.SIGNIN_COLLECTORS) <= set(REGISTRY)


def test_resolved_findings_are_pruned_after_their_period(db, two_tenants):
    alpha, _ = two_tenants
    first_scan = finding(db, alpha, "first", iso(500), resolved_at=iso(400))  # marks the first scan: kept
    old = finding(db, alpha, "old", iso(300), resolved_at=iso(200))
    recent = finding(db, alpha, "recent", iso(300), resolved_at=iso(20))
    still_open = finding(db, alpha, "open", iso(300))
    assert retention.dry_run(db, retention.Policy(findings_days=90))["findings_days"] == 1
    assert retention.prune_findings(db, 90) == 1
    left = {r[0] for r in db.execute("SELECT id FROM findings")}
    assert left == {first_scan, recent, still_open} and old not in left
    assert retention.prune_findings(db, None) == 0


def test_accepted_risks_are_never_pruned(db, two_tenants):
    alpha, _ = two_tenants
    finding(db, alpha, "first", iso(500), resolved_at=iso(400))
    accepted = finding(db, alpha, "accepted", iso(300), resolved_at=iso(200), accepted_at=iso(250))
    assert retention.dry_run(db, retention.Policy(findings_days=90))["findings_days"] == 0
    assert retention.prune_findings(db, 90) == 0
    assert db.execute("SELECT 1 FROM findings WHERE id = ?", (accepted,)).fetchone()


def test_run_logs_follow_their_setting(db, cfg, two_tenants):
    alpha, _ = two_tenants
    rid = store.enqueue_run(db, alpha, "users")
    store.log_event(db, rid, "info", "line")
    store.log_event(db, rid, "error", "kept")
    store.finish_run(db, rid, "succeeded")
    db.execute("UPDATE runs SET created_at = ? WHERE id = ?", (iso(40), rid))
    assert retention.dry_run(db, retention.Policy(run_log_days=90))["run_log_days"] == 0
    assert retention.dry_run(db, retention.Policy(run_log_days=30))["run_log_days"] == 1
    db.execute("INSERT INTO settings (key, value) VALUES ('retention.run_log_days', '30')")
    done = retention.apply(db, cfg)
    assert done["run_log_days"] == 1
    assert [r[0] for r in db.execute("SELECT level FROM run_events WHERE run_id = ?", (rid,))] == ["error"]


def test_issued_reports_keep_the_floor_and_the_latest(db, two_tenants):
    alpha, bravo = two_tenants
    old = issued(db, alpha, "review", 800)
    within = issued(db, alpha, "review", 300)
    latest_kind = issued(db, alpha, "evidence", 900)  # the only evidence pack: month-end needs it
    issued(db, bravo, "review", 1)
    assert retention.dry_run(db, retention.Policy(issued_days=400))["issued_days"] == 1
    assert retention.prune_issued(db, 30) == 1  # 30 days is raised to the 365-day floor
    left = {r[0] for r in db.execute("SELECT id FROM issued_reports")}
    assert old not in left and {within, latest_kind} <= left


def test_every_file_of_the_latest_send_is_kept(db, two_tenants):
    alpha, _ = two_tenants
    older = [issued(db, alpha, "review", 900, "r.pdf"), issued(db, alpha, "review", 900, "r.xlsx")]
    latest = [issued(db, alpha, "review", 800, "r.pdf"), issued(db, alpha, "review", 800, "r.xlsx")]
    assert retention.dry_run(db, retention.Policy(issued_days=400))["issued_days"] == 2
    assert retention.prune_issued(db, 400) == 2
    left = {r[0] for r in db.execute("SELECT id FROM issued_reports")}
    assert left == set(latest) and not left & set(older)


def test_snapshot_settings_reach_the_worker(db, cfg, two_tenants):
    alpha, _ = two_tenants
    runs = [run_with(db, alpha, "users", d) for d in (2, 3, 4, 5)]
    db.execute("INSERT INTO settings (key, value) VALUES ('retention.keep_daily', '2')")
    db.execute("INSERT INTO settings (key, value) VALUES ('retention.keep_monthly', '0')")
    policy = retention.load(db, cfg)
    assert retention.dry_run(db, policy)["snapshots"] == 2
    assert worker.prune(db, cfg) == 2
    assert [bool(kinds_of(db, r)) for r in runs] == [True, True, False, False]


def test_keep_every_month_forever(db, cfg, two_tenants):
    alpha, _ = two_tenants
    runs = [run_with(db, alpha, "users", 31 * m + 1) for m in range(30)]
    db.execute("INSERT INTO settings (key, value) VALUES ('retention.keep_daily', '1')")
    db.execute("INSERT INTO settings (key, value) VALUES ('retention.keep_monthly', 'forever')")
    assert retention.load(db, cfg).keep_monthly is None
    worker.prune(db, cfg)
    assert all(kinds_of(db, r) for r in runs)  # one run in each of 30 different months
