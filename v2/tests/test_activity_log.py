"""The audit log: source addresses, no flooding by strangers, retention, and the admin Activity log page."""

from datetime import UTC, datetime, timedelta

from officesentry import store
from officesentry.db import AUDIT_NOISE_DAYS, audit, prune_audit
from tests.conftest import PASSWORD, login, make_user

ATTACKER = "198.51.100.7"


def _post_login(client, username, password, ip=ATTACKER):
    return client.post("/login", data={"username": username, "password": password}, environ_base={"REMOTE_ADDR": ip})


def test_failed_sign_ins_with_unknown_names_cant_flood_the_log(client, db):
    make_user(db, "admin1")
    before = db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    for i in range(30):
        _post_login(client, f"nobody{i}" + "x" * 100, "hunter2-typed-in-the-wrong-box")
    rows = db.execute("SELECT * FROM audit_log ORDER BY id").fetchall()[before:]
    # One counted row for the failures and one for the attempts that were then slowed down, not 30.
    assert [(r["action"], r["detail"], r["ip"], r["username"]) for r in rows] == [
        ("login_failed_unknown", "20 attempts", ATTACKER, None),
        ("login_throttled", "10 attempts", ATTACKER, None),
    ]
    assert not any("nobody" in str(tuple(r)) for r in rows)


def test_audit_rows_record_the_address(client, db):
    make_user(db, "admin")
    _post_login(client, "admin", PASSWORD, ip="203.0.113.20")
    row = db.execute("SELECT * FROM audit_log WHERE action = 'login'").fetchone()
    assert row["ip"] == "203.0.113.20"
    _post_login(client, "admin", "wrong password", ip="203.0.113.21")
    audit(db, "cli", "keys_rotated")  # outside a web request: no address
    assert db.execute("SELECT ip FROM audit_log WHERE action = 'keys_rotated'").fetchone()[0] is None


def test_old_sign_in_noise_is_pruned_but_changes_are_kept(db):
    old = (datetime.now(UTC) - timedelta(days=AUDIT_NOISE_DAYS + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.executemany(
        "INSERT INTO audit_log (ts, username, action) VALUES (?, ?, ?)",
        [
            (old, "a", "login_failed"),
            (old, None, "login_failed_unknown"),
            (old, "a", "user_updated"),
            (old, "a", "account_locked"),
        ],
    )
    audit(db, "a", "login_failed")  # recent
    assert prune_audit(db) == 2
    assert sorted(r[0] for r in db.execute("SELECT action FROM audit_log")) == [
        "account_locked",
        "login_failed",
        "user_updated",
    ]


def _seed(db, connection_id):
    alpha = store.create_tenant(db, "Alpha Ltd", "alpha.onmicrosoft.com", connection_id)
    rows = [
        ("2026-09-01T10:00:00Z", "ana", "collect_requested", alpha, "users"),
        ("2026-09-02T10:00:00Z", "bob", "tenant_updated", alpha, '=HYPERLINK("x")'),
        ("2026-09-03T10:00:00Z", "ana", "login", None, ""),
        ("2026-09-04T10:00:00Z", "ana", "tenant_deleted", 999, "Gone Ltd (gone.example.com)"),
    ]
    db.executemany(
        "INSERT INTO audit_log (ts, username, action, tenant_id, detail, ip) VALUES (?, ?, ?, ?, ?, '192.0.2.1')", rows
    )
    return alpha


def test_activity_log_is_admin_only(client, db, connection_id):
    make_user(db, "ana", "analyst", all_tenants=True)
    login(client, "ana")
    assert client.get("/admin/activity").status_code == 403
    assert client.get("/admin/activity.csv").status_code == 403


def test_activity_log_filters_pages_and_exports(client, db, connection_id):
    alpha = _seed(db, connection_id)
    make_user(db, "boss")
    login(client, "boss")
    page = client.get("/admin/activity").get_data(as_text=True)
    assert "Activity log" in page and "Alpha Ltd" in page and "192.0.2.1" in page
    assert "Tenant #999 (deleted or not visible)" in page

    page = client.get("/admin/activity", query_string={"user": "ana", "action": "collect_requested"}).get_data(
        as_text=True
    )
    assert "Collect requested" in page and "Tenant updated" not in page.split("<tbody>")[1]
    page = client.get(
        "/admin/activity", query_string={"tenant": alpha, "from": "2026-09-02", "to": "2026-09-02"}
    ).get_data(as_text=True)
    body = page.split("<tbody>")[1]
    assert "Tenant updated" in body and "Collect requested" not in body

    for i in range(60):
        audit(db, "ana", "report_downloaded", detail=f"report {i}")
    first = client.get("/admin/activity", query_string={"user": "ana"}).get_data(as_text=True)
    assert "report 59" in first and "report 5<" not in first and "page=2" in first and "user=ana" in first
    second = client.get("/admin/activity", query_string={"user": "ana", "page": 2}).get_data(as_text=True)
    assert "report 5<" in second and "Newer" in second

    csv = client.get("/admin/activity.csv", query_string={"tenant": alpha}).get_data(as_text=True)
    lines = csv.strip().splitlines()
    assert lines[0].lstrip("﻿") == "Time (UTC),User,Action,Tenant,Address,Detail"
    assert len(lines) == 3 and "'=HYPERLINK" in csv  # formulas are defused
    assert db.execute("SELECT 1 FROM audit_log WHERE action = 'activity_log_downloaded'").fetchone()
