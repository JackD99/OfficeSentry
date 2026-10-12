"""What a role can't open on a client stays closed wherever that client's data goes: scheduled emails and
their previews, the report history, report designs, month-end notes and the home pages.

A role holds report areas (``reports.identity``, ``reports.security``, ...), and an admin can take one away
from a person on one client. ``tests/test_roles.py`` checks each page's own gate; these are the places that
show or send a report from inside another page, each with the request that used to get through.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest

from officesentry import delivery, month_end, roles
from officesentry.reports import designs
from tests.conftest import login, make_user
from tests.seed import seed_tenant
from tests.test_email import _smtp_ready, fake_smtp, sends  # noqa: F401 (autouse fixture)
from tests.test_roles import role_user, store_user

MARK = "Zebedee"  # in every name of the seeded tenant's people
NOW = datetime.now(UTC)
PERIOD = f"{NOW.year}-{NOW.month:02d}"
FORM = {"frequency": "monthly", "day_of_month": "1", "hour": "8", "timezone": "UTC", "include_xlsx": "1"}


@pytest.fixture
def alpha(db, two_tenants):
    alpha, _ = two_tenants
    db.execute("UPDATE tenants SET consented_at = '2026-01-01T00:00:00Z'")
    seed_tenant(db, alpha, mark=MARK)
    _smtp_ready(db)
    return alpha


def _deny(db, uid: int, capability: str, tenant_id: int) -> None:
    roles.set_overrides(db, "admin", store_user(db, uid), [roles.Override(capability, tenant_id, False)])


def _switch(client, username: str) -> None:
    client.post("/logout")
    login(client, username)


def _schedule(db, tenant_id: int, reports: list[str], email: str = "it@alpha.example") -> int:
    contact = delivery.add_contact(db, tenant_id, "IT", email)
    sid = db.execute(
        "INSERT INTO email_schedules (kind, tenant_id, reports, include_xlsx, frequency, day_of_month, hour,"
        " timezone, created_by, created_at) VALUES ('report', ?, ?, 1, 'monthly', 1, 8, 'UTC', 'admin',"
        " '2026-01-01T00:00:00Z')",
        (tenant_id, json.dumps(reports)),
    ).lastrowid
    delivery.set_recipients(db, "schedule", sid, [contact])
    return sid


def test_a_schedule_only_sends_reports_its_maker_can_open(client, db, alpha):
    """An account manager doesn't hold identity reports: the form doesn't offer them, and naming one, or a
    saved view of one, sends nothing of it."""
    role_user(db, "am", "account_manager", tenants=[alpha])
    login(client, "am")
    assert client.get(f"/tenants/{alpha}/data/all-users").status_code == 403
    form = client.get(f"/tenants/{alpha}/schedules/new").data.decode()
    assert 'value="data:all-users"' not in form and 'value="data:licences"' in form
    client.post("/reports/all-users/views", data={"tenant_id": str(alpha), "name": "Everyone", "query": "hide=x/y"})
    view = db.execute("SELECT id FROM saved_views WHERE report_key = 'all-users'").fetchone()[0]
    client.post(
        f"/tenants/{alpha}/schedules/new",
        data={
            **FORM,
            "reports": ["data:all-users", "data:licences"],
            "views": f"s{view}",
            f"view_format_s{view}": "xlsx",
            "new_email": "me@home.example",
        },
    )
    s = db.execute("SELECT * FROM email_schedules WHERE tenant_id = ?", (alpha,)).fetchone()
    assert json.loads(s["reports"]) == ["data:licences"]
    assert delivery.schedule_views(db, s["id"]) == []


def test_a_schedule_sending_more_than_you_open_only_changes_for_someone_who_can(client, db, alpha):
    """Changing who it goes to would hand over the rest, so the schedule is left as it is."""
    sid = _schedule(db, alpha, ["review", "data:all-users"])
    role_user(db, "am", "account_manager", tenants=[alpha])
    login(client, "am")
    resp = client.get(f"/schedules/{sid}/edit", follow_redirects=True)
    assert "Only someone who can open every report this email sends can change it." in resp.data.decode()
    client.post(f"/schedules/{sid}/edit", data={**FORM, "reports": ["review"], "new_email": "me@home.example"})
    assert [c["email"] for c in delivery.recipients(db, "schedule", sid)] == ["it@alpha.example"]
    assert json.loads(db.execute("SELECT reports FROM email_schedules").fetchone()[0]) == [
        "review",
        "data:all-users",
    ]
    # Someone who opens every report it sends still changes it.
    make_user(db, "admin")
    _switch(client, "admin")
    assert client.get(f"/schedules/{sid}/edit").status_code == 200


def test_report_history_needs_the_report_area_and_downloads(client, db, alpha):
    sid = _schedule(db, alpha, ["data:all-users", "data:licences"])
    make_user(db, "admin")
    login(client, "admin")
    assert client.post(f"/schedules/{sid}/send").status_code == 302
    files = {
        r["kind"]: r["id"] for r in db.execute("SELECT kind, id FROM issued_reports WHERE tenant_id = ?", (alpha,))
    }
    assert set(files) == {"data:all-users", "data:licences"}
    role_user(db, "am", "account_manager", tenants=[alpha])
    _switch(client, "am")
    page = client.get(f"/tenants/{alpha}/report-history").data.decode()
    assert (
        f"report-history/{files['data:licences']}" in page and f"report-history/{files['data:all-users']}" not in page
    )
    assert client.get(f"/tenants/{alpha}/report-history/{files['data:all-users']}").status_code == 403
    assert client.get(f"/tenants/{alpha}/report-history/{files['data:licences']}").status_code == 200
    # Downloads taken away on this client: the files are listed, not downloadable.
    viewer = make_user(db, "it@alpha.example", role="client", tenants=[alpha])
    _deny(db, viewer, "export", alpha)
    _switch(client, "it@alpha.example")
    page = client.get(f"/tenants/{alpha}/report-history").data.decode()
    assert "report-history/" not in page and "All users" in page
    assert client.get(f"/tenants/{alpha}/report-history/{files['data:licences']}").status_code == 403


def test_email_previews_need_security_findings(client, db, alpha):
    """Every report email opens with the client's security summary, and an alert lists findings."""
    sid = _schedule(db, alpha, ["data:mailboxes"])
    aid = db.execute(
        "INSERT INTO email_alerts (tenant_id, min_severity, created_by, created_at) VALUES (?, 'high', 'admin',"
        " '2026-01-01T00:00:00Z')",
        (alpha,),
    ).lastrowid
    role_user(db, "fl", "first_line", tenants=[alpha])
    login(client, "fl")
    page = client.get(f"/tenants/{alpha}/schedules").data.decode()
    assert f"/schedules/{sid}/preview" not in page and f"/alerts/{aid}/preview" not in page
    for url in (f"/schedules/{sid}/preview", f"/schedules/{sid}/preview/body", f"/alerts/{aid}/preview"):
        assert client.get(url).status_code == 403, url


def test_draft_commentary_stays_out_of_emails_while_reports_wait_for_approval(client, db, alpha, monkeypatch):
    """A client on a send-on-approval plan gets the month's commentary once it's approved, also in the
    review attached to any other scheduled email."""
    pages = []
    monkeypatch.setattr(delivery.export, "to_pdf", lambda html, base: pages.append(html) or html.encode())
    plan = month_end.save_plan(
        db,
        {
            "name": "Monthly",
            "reports": ["review"],
            "include_xlsx": 0,
            "day_of_month": 1,
            "hour": 8,
            "timezone": "UTC",
            "subject": "",
            "message": "",
            "mode": "approval",
            "delivery": "email",
            "bcc_team": 0,
        },
        "admin",
    )
    month_end.apply_plan(db, month_end.get_plan(db, plan), [alpha], "admin", "UTC")
    month_end.update_review(db, alpha, PERIOD, "sl", state="in_review", commentary="Still being written.")
    sid = _schedule(db, alpha, ["review"])
    make_user(db, "admin")
    login(client, "admin")
    client.post(f"/schedules/{sid}/send")
    assert pages and not any("Still being written." in p for p in pages)
    month_end.update_review(db, alpha, PERIOD, "admin", state="approved")
    pages.clear()
    db.execute("DELETE FROM email_deliveries")
    client.post(f"/schedules/{sid}/send")
    assert any("Still being written." in p for p in pages)


def test_clients_never_see_who_on_the_team_sent_an_email(client, db, alpha):
    sid = _schedule(db, alpha, ["data:mailboxes"])
    make_user(db, "jane.admin", role="admin")
    login(client, "jane.admin")
    client.post(f"/schedules/{sid}/send")
    assert b"sent by jane.admin" in client.get(f"/tenants/{alpha}/schedules").data
    make_user(db, "it@alpha.example", role="client", tenants=[alpha])
    _switch(client, "it@alpha.example")
    page = client.get(f"/tenants/{alpha}/schedules").data.decode()
    assert "it@alpha.example" in page and "jane.admin" not in page


def test_staff_notes_follow_month_end_review_on_each_client(client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    db.execute(
        "INSERT INTO finding_notes (tenant_id, rule_key, body, visibility, created_at)"
        " VALUES (?, 'admin_mfa', 'Only for the team.', 'staff', '2026-01-01T00:00:00Z')",
        (alpha,),
    )
    month_end.update_review(db, alpha, PERIOD, "x", state="in_review", commentary="A draft.")
    uid = role_user(db, "sl", "second_line", tenants=[alpha, bravo])
    _deny(db, uid, "month_end.review", alpha)
    login(client, "sl")
    page = client.get(f"/tenants/{alpha}/report").data.decode()
    assert "Only for the team." not in page and "A draft." not in page


def _design(db, *blocks) -> int:
    return designs.create(db, "Board pack", "test", {"settings": {}, "blocks": list(blocks)})


def test_designs_leave_out_what_the_viewer_cant_open(client, db, alpha, two_tenants):
    table = {"kind": "data_table", "settings": {"report": "all-users", "max_rows": 50}}
    did = _design(db, {"kind": "findings", "settings": {"severities": ["critical", "high"]}}, table)
    # An account manager holds security findings but not identity reports.
    role_user(db, "am", "account_manager", tenants=[alpha])
    login(client, "am")
    assert MARK.encode() not in client.get(f"/tenants/{alpha}/designs/{did}").data
    # Second line with identity taken away on Alpha previews it there without the table, and with security
    # taken away on Bravo can't preview it on Bravo.
    _, bravo = two_tenants
    seed_tenant(db, bravo)
    uid = role_user(db, "sl", "second_line", tenants=[alpha, bravo])
    roles.set_overrides(
        db,
        "admin",
        store_user(db, uid),
        [roles.Override("reports.identity", alpha, False), roles.Override("reports.security", bravo, False)],
    )
    _switch(client, "sl")
    only_table = _design(db, table)
    page = client.get(f"/reporting/designs/{only_table}/preview?tenant={alpha}").data.decode()
    assert MARK not in page and "Left out: you can&#39;t open this report for this client." in page
    page = client.get(f"/reporting/designs/{did}/preview?tenant={bravo}").data.decode()
    assert "Bravo Inc" not in page


def _tile(body: str, label: str) -> str:
    m = re.search(re.escape(label) + r".*?<div class=\"value[^\"]*\">\s*(?:<span>)?([^<]*)", body, re.S)
    return m.group(1).strip() if m else ""


def test_home_pages_count_only_what_the_person_sees(client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, bravo)
    uid = role_user(db, "sl", "second_line", tenants=[alpha, bravo])
    roles.set_overrides(
        db,
        "admin",
        store_user(db, uid),
        [roles.Override("reports.security", bravo, False), roles.Override("projects.view", bravo, False)],
    )
    login(client, "sl")
    assert _tile(client.get("/").data.decode(), "Critical and high findings") == "0"
    accounts = client.get("/clients").data.decode()
    assert "Bravo Inc" in accounts and f"/tenants/{bravo}/projects" not in accounts
    # First line holds no security findings: Today doesn't count them.
    role_user(db, "fl", "first_line", tenants=[bravo])
    _switch(client, "fl")
    today = client.get("/today").data.decode()
    assert _tile(today, "New critical and high") == "0" and "Failing:" not in today


def test_a_client_home_leaves_out_areas_taken_away(client, db, alpha):
    uid = make_user(db, "it@alpha.example", role="client", tenants=[alpha])
    login(client, "it@alpha.example")
    assert b"<h2>Recommendations</h2>" in client.get("/").data
    _deny(db, uid, "reports.security", alpha)  # which signs them out
    _switch(client, "it@alpha.example")
    page = client.get("/").data.decode()
    assert "<h2>Recommendations</h2>" not in page and "Checks needing attention" not in page
    assert f"/tenants/{alpha}/findings" not in page and "Users with MFA" in page
