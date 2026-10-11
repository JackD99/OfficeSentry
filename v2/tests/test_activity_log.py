"""The audit log: source addresses, no flooding by strangers, retention, and the admin Activity log page."""

import re
from datetime import UTC, datetime, timedelta

from officesentry import store
from officesentry.db import AUDIT_NOISE, AUDIT_NOISE_DAYS, audit, audit_counted, prune_audit
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
    assert "Client #999 (deleted or not visible)" in page

    page = client.get("/admin/activity", query_string={"user": "ana", "action": "collect_requested"}).get_data(
        as_text=True
    )
    assert "Collect requested" in page and "Client updated" not in page.split("<tbody>")[1]
    page = client.get(
        "/admin/activity", query_string={"tenant": alpha, "from": "2026-09-02", "to": "2026-09-02"}
    ).get_data(as_text=True)
    body = page.split("<tbody>")[1]
    assert "Client updated" in body and "Collect requested" not in body

    for i in range(60):
        audit(db, "ana", "report_downloaded", detail=f"report {i}")
    first = client.get("/admin/activity", query_string={"user": "ana"}).get_data(as_text=True)
    assert "report 59" in first and "report 5<" not in first and "page=2" in first and "user=ana" in first
    second = client.get("/admin/activity", query_string={"user": "ana", "page": 2}).get_data(as_text=True)
    assert "report 5<" in second and "Newer" in second

    csv = client.get("/admin/activity.csv", query_string={"tenant": alpha}).get_data(as_text=True)
    lines = csv.strip().splitlines()
    assert lines[0].lstrip("﻿") == "Time (UTC),User,Action,Client,Address,Detail"
    assert len(lines) == 3 and "'=HYPERLINK" in csv  # formulas are defused
    assert db.execute("SELECT 1 FROM audit_log WHERE action = 'activity_log_downloaded'").fetchone()


def test_refused_pages_are_counted_per_person_and_hour(client, db, two_tenants):
    alpha, bravo = two_tenants
    make_user(db, "someone", role="client", tenants=[alpha])
    login(client, "someone")
    assert client.get(f"/tenants/{bravo}").status_code == 404  # another client's tenant
    assert client.get(f"/tenants/{bravo}/report").status_code == 404
    assert client.get("/admin/users").status_code == 403  # an admin page
    assert client.get("/across").status_code == 403  # a staff page
    assert client.get(f"/tenants/{alpha}").status_code in (200, 302)  # their own: not a refusal
    rows = db.execute("SELECT * FROM audit_log WHERE action = 'access_refused'").fetchall()
    assert len(rows) == 1
    assert rows[0]["username"] == "someone" and rows[0]["ip"] == "127.0.0.1"
    assert rows[0]["detail"] == "4 attempts, last GET /across"
    assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'access_refused'").fetchone()[0] == 1
    # The first refusal names what was refused.
    db.execute("DELETE FROM audit_log")
    client.get(f"/tenants/{bravo}/findings")
    assert db.execute("SELECT detail FROM audit_log WHERE action = 'access_refused'").fetchone()[0] == (
        f"1 attempt, last GET /tenants/{bravo}/findings (tenant {bravo})"
    )


def test_refusals_show_in_the_activity_log(client, db, two_tenants):
    alpha, bravo = two_tenants
    make_user(db, "admin")
    audit_counted(db, "access_refused", "10.0.0.9", "someone", f"GET /tenants/{bravo} (tenant {bravo})")
    login(client, "admin")
    page = client.get("/admin/activity?action=access_refused").data.decode()
    assert "Access refused" in page and "someone" in page and f"GET /tenants/{bravo}" in page


def test_a_stale_form_token_is_counted_and_explained(cfg, db, two_tenants):
    from officesentry.web import create_app

    cfg.extra = {}  # CSRF on, as in production
    app = create_app(cfg)
    make_user(db, "someone", role="admin")
    anonymous = app.test_client()
    resp = anonymous.post("/login", data={"username": "someone", "password": PASSWORD})
    assert resp.status_code == 400
    page = resp.data.decode()
    assert "That form couldn" in page and "reload the page" in page and "CSRF" not in page
    rows = db.execute("SELECT * FROM audit_log WHERE action = 'csrf_failed'").fetchall()
    assert len(rows) == 1 and rows[0]["username"] is None and rows[0]["detail"] == "1 attempt, last /login"
    # Signed in (through the real form), a stale token on a later form is counted against the person.
    browser = app.test_client()
    html = browser.get("/login").data.decode()
    token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    assert (
        browser.post("/login", data={"username": "someone", "password": PASSWORD, "csrf_token": token}).status_code
        == 302
    )
    assert browser.post("/account/password", data={"current": PASSWORD}).status_code == 400
    row = db.execute("SELECT * FROM audit_log WHERE action = 'csrf_failed' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["username"] == "someone" and row["detail"] == "1 attempt, last /account/password"
    # It's sign-in noise: pruned with the rest after AUDIT_NOISE_DAYS.
    assert "csrf_failed" in AUDIT_NOISE
