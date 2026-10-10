"""Month-end reporting: report plans, coverage, the review queue, commentary and notes, approval."""

from datetime import UTC, datetime

from officesentry import delivery, lifecycle, month_end, store, tasks
from officesentry.db import connect
from tests.conftest import login, make_user
from tests.seed import seed_tenant
from tests.test_email import _smtp_ready, fake_smtp, sends  # noqa: F401 (autouse fixture)

NOW = datetime.now(UTC)
PERIOD = f"{NOW.year}-{NOW.month:02d}"  # the server timezone in tests is UTC


def _plan(client, tenants, mode="approval", delivery_="email", **extra):
    data = {
        "name": "Monthly",
        "reports": ["review"],
        "include_xlsx": "1",
        "day_of_month": "1",
        "hour": "8",
        "timezone": "UTC",
        "subject": "{client} report for {month}",
        "message": "Hello {client}",
        "mode": mode,
        "delivery": delivery_,
        "tenants": [str(t) for t in tenants],
    }
    data.update(extra)
    return client.post("/reporting/plans/new", data=data)


def _admin(client, db):
    make_user(db, "admin")
    login(client, "admin")


def work(client) -> int:
    """What the worker's task threads do: run every queued task (approvals) to the end."""
    app = client.application
    cfg = app.extensions["officesentry.config"]
    conn = connect(cfg.db_path)
    try:
        return tasks.drain(app, conn, app.extensions["officesentry.vault"], cfg)
    finally:
        conn.close()


def test_plan_creates_schedules_and_coverage_counts_gaps(client, db, two_tenants, connection_id):
    alpha, bravo = two_tenants
    charlie = store.create_tenant(db, "Charlie Co", "charlie.onmicrosoft.com", connection_id)
    a_contact = delivery.add_contact(db, alpha, "IT", "it@alpha.com")
    _admin(client, db)
    assert _plan(client, [alpha, bravo]).status_code == 302
    rows = db.execute("SELECT * FROM email_schedules ORDER BY tenant_id").fetchall()
    assert [r["tenant_id"] for r in rows] == [alpha, bravo]
    for r in rows:
        assert r["frequency"] == "monthly" and r["on_approval"] == 1 and r["plan_id"]
        assert r["subject"] == "{client} report for {month}" and r["last_period"]  # never sends straight away
    assert [c["id"] for c in delivery.recipients(db, "schedule", rows[0]["id"])] == [a_contact]
    assert delivery.recipients(db, "schedule", rows[1]["id"]) == []
    # A contact added later joins the plan email.
    client.post(f"/tenants/{bravo}/contacts", data={"name": "", "email": "it@bravo.com"})
    assert [c["email"] for c in delivery.recipients(db, "schedule", rows[1]["id"])] == ["it@bravo.com"]

    cov = month_end.coverage(db, db.execute("SELECT * FROM tenants ORDER BY id").fetchall())
    assert (cov.total, cov.covered) == (3, 2)
    gaps = {r.tenant["id"]: r.gaps for r in cov.gaps}
    assert gaps == {charlie: ["no_report", "no_contacts"]}
    assert cov.headline == "2 of 3 clients have a monthly report; 1 has no contacts"
    page = client.get("/emails").get_data(as_text=True)
    assert "2 of 3 clients have a monthly report" in page and f"/reporting/plans/new?tenant={charlie}" in page
    assert f"/tenants/{charlie}/schedules#contacts" in page

    plans_page = client.get("/reporting/plans").get_data(as_text=True)
    assert "Monthly" in plans_page and "Send on approval" in plans_page and "2 of 3 clients" in plans_page
    form = client.get(f"/reporting/plans/new?tenant={charlie}").get_data(as_text=True)
    assert f'value="{charlie}" checked' in form and "No contacts yet" in form
    assert "Sends on approval" in client.get(f"/tenants/{alpha}/schedules").get_data(as_text=True)

    # Re-applying with one tenant removes the other's plan schedule; editing re-times the rest.
    plan_id = db.execute("SELECT id FROM report_plans").fetchone()[0]
    client.post(
        f"/reporting/plans/{plan_id}",
        data={
            "name": "Monthly",
            "reports": ["review"],
            "day_of_month": "3",
            "hour": "9",
            "timezone": "",
            "subject": "",
            "message": "",
            "mode": "auto",
            "delivery": "email",
            "tenants": [str(alpha)],
        },
    )
    rows = db.execute("SELECT * FROM email_schedules").fetchall()
    assert [(r["tenant_id"], r["day_of_month"], r["on_approval"]) for r in rows] == [(alpha, 3, 0)]
    assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action LIKE 'report_plan_%'").fetchone()[0] == 2


def test_subject_and_message_placeholders(app, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    plan_id = month_end.save_plan(
        db,
        {
            "name": "P",
            "reports": ["review"],
            "include_xlsx": 0,
            "day_of_month": 1,
            "hour": 8,
            "timezone": "UTC",
            "subject": "{client}: {month} review",
            "message": "Dear {client}",
            "mode": "auto",
            "delivery": "email",
            "bcc_team": 0,
        },
        "admin",
    )
    month_end.apply_plan(db, month_end.get_plan(db, plan_id), [alpha], "admin", "UTC")
    s = db.execute("SELECT * FROM email_schedules").fetchone()
    t = store.get_tenant(db, alpha)
    built = delivery.build_report_email(app, db, s, t, [], now=datetime(2026, 10, 2, tzinfo=UTC), attach=False)
    assert built.email.subject == "Alpha Ltd: October 2026 review"
    assert "Dear Alpha Ltd" in built.email.text


def test_send_on_approval_schedule_does_not_send_on_timer(app, db, two_tenants, vault, cfg, monkeypatch):
    alpha, bravo = two_tenants
    pdf_html = []
    monkeypatch.setattr(delivery.export, "to_pdf", lambda html, base: pdf_html.append(html) or b"%PDF-1.7")
    # An automatic plan's PDF carries the month's commentary too.
    month_end.update_review(db, bravo, "2026-10", "admin", commentary="Bravo had a good month.")
    seed_tenant(db, alpha)
    seed_tenant(db, bravo)
    _smtp_ready(db)
    for tenant_id in (alpha, bravo):
        delivery.add_contact(db, tenant_id, "", f"it@{tenant_id}.com")
    plan = {
        "name": "P",
        "reports": ["review"],
        "include_xlsx": 0,
        "day_of_month": 1,
        "hour": 0,
        "timezone": "UTC",
        "subject": "",
        "message": "",
        "delivery": "email",
        "bcc_team": 0,
    }
    approval = month_end.save_plan(db, dict(plan, mode="approval"), "admin")
    auto = month_end.save_plan(db, dict(plan, name="Q", mode="auto"), "admin")
    month_end.apply_plan(
        db, month_end.get_plan(db, approval), [alpha], "admin", "UTC", now=datetime(2026, 9, 15, tzinfo=UTC)
    )
    month_end.apply_plan(
        db, month_end.get_plan(db, auto), [bravo], "admin", "UTC", now=datetime(2026, 9, 15, tzinfo=UTC)
    )
    now = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
    db.execute("UPDATE runs SET finished_at = ?", (now.strftime("%Y-%m-%dT%H:%M:%SZ"),))  # fresh: no refresh
    assert delivery.tick(app, db, vault, cfg, now) == 1
    ((_, _, msg, _),) = sends()
    assert msg["To"] == f"it@{bravo}.com"
    assert len(pdf_html) == 1 and "Bravo had a good month." in pdf_html[0]
    # Its Send now and Retry don't bypass approval either.
    s = db.execute("SELECT * FROM email_schedules WHERE tenant_id = ?", (alpha,)).fetchone()
    assert "approved" in delivery.retry_delivery(app, db, vault, {"schedule_id": s["id"]}, "admin")


def test_queue_lists_planned_tenants_with_states_and_flags(client, db, two_tenants, connection_id):
    alpha, bravo = two_tenants
    store.create_tenant(db, "Not Planned", "np.onmicrosoft.com", connection_id)
    seed_tenant(db, alpha)
    _admin(client, db)
    _plan(client, [alpha, bravo])
    month_end.update_review(db, alpha, PERIOD, "admin", state="in_review", reviewer="admin")
    page = client.get(f"/reporting/{PERIOD}").get_data(as_text=True)
    assert "Alpha Ltd" in page and "Bravo Inc" in page and "Not Planned" not in page
    assert "In review" in page and "Draft" in page
    assert "No data collected yet" in page  # Bravo has never been collected
    rows = {r.tenant["name"]: r for r in month_end.queue(db, db.execute("SELECT * FROM tenants").fetchall(), PERIOD)}
    assert rows["Alpha Ltd"].state == "in_review" and rows["Bravo Inc"].state == "draft"
    assert rows["Alpha Ltd"].verdict.label and rows["Bravo Inc"].flags == ["No data collected yet"]
    mine = client.get(f"/reporting/{PERIOD}?show=mine").get_data(as_text=True)
    assert "Alpha Ltd" in mine and "Bravo Inc" not in mine
    assert client.get("/reporting").headers["Location"].endswith(f"/reporting/{PERIOD}")
    assert client.get("/reporting/2026-13").status_code == 404


def test_review_suggests_projects_then_quick_fixes(client, db, two_tenants):
    """Projects are half a day or more; smaller fixes are offered separately, and nothing is offered twice."""
    from officesentry import projects

    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "analyst", role="analyst", all_tenants=True)
    login(client, "analyst")
    offered = month_end.suggestions(db, store.get_tenant(db, alpha))
    assert [s["rule_key"] for s in offered if s["kind"] == "project"] == ["project:identity", "project:email_security"]
    quick = [s for s in offered if s["kind"] == "quick"]
    assert quick[0]["rule_key"] == "admin_mfa" and quick[0]["severity"] == "critical"
    assert "legacy_auth_blocked" not in {s["rule_key"] for s in quick}  # part of the identity project
    review = client.get(f"/reporting/{PERIOD}/{alpha}").get_data(as_text=True)
    assert (
        review.index("Projects to propose")
        < review.index("Identity and Conditional Access project")
        < review.index("Quick fixes")
        < review.index("Have each listed admin register a strong method")
    )
    client.post(f"/tenants/{alpha}/report-actions", data={"rule_key": "project:identity", "period": PERIOD})
    action = month_end.actions(db, alpha)[0]
    assert action["title"] == "Identity and Conditional Access project"
    assert action["detail"] == projects.PROJECTS["identity"].pitch and action["rule_key"] == "project:identity"
    review = client.get(f"/reporting/{PERIOD}/{alpha}").get_data(as_text=True)
    assert 'value="project:identity"' not in review and 'value="project:email_security"' in review
    # A declined project isn't offered.
    projects.set_status(db, alpha, "email_security", "declined", "", "ann")
    offered = month_end.suggestions(db, store.get_tenant(db, alpha))
    assert "project:email_security" not in {s["rule_key"] for s in offered}
    assert (
        client.post(f"/tenants/{alpha}/report-actions", data={"rule_key": "project:nope", "period": PERIOD}).status_code
        == 400
    )


def test_commentary_actions_and_notes_on_report(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "analyst", role="analyst", all_tenants=True)
    login(client, "analyst")
    client.post(f"/reporting/{PERIOD}/{alpha}/commentary", data={"commentary": "A quiet month overall."})
    assert month_end.state_of(db, alpha, PERIOD) == "in_review"
    review = client.get(f"/reporting/{PERIOD}/{alpha}").get_data(as_text=True)
    report = month_end.monthly_review.build(db, store.get_tenant(db, alpha))
    rule = report.failing[0]
    assert rule.action in review  # suggestions use the check's action wording
    client.post(f"/tenants/{alpha}/report-actions", data={"rule_key": rule.key, "period": PERIOD})
    client.post(
        f"/tenants/{alpha}/report-actions",
        data={"title": "Book a licence review", "period": PERIOD, "target_month": "2026-12"},
    )
    first, second = month_end.actions(db, alpha)
    assert first["title"] == rule.action and first["rule_key"] == rule.key
    client.post(f"/report-actions/{second['id']}", data={"op": "up"})
    client.post(f"/report-actions/{first['id']}", data={"op": "save", "status": "waiting", "target_month": ""})
    assert [a["title"] for a in month_end.actions(db, alpha)] == ["Book a licence review", rule.action]
    client.post(
        f"/tenants/{alpha}/finding-notes",
        data={"rule_key": rule.key, "body": "Client is buying P1", "visibility": "client"},
    )
    client.post(
        f"/tenants/{alpha}/finding-notes",
        data={"rule_key": rule.key, "body": "Chase Dave internally", "visibility": "staff"},
    )

    page = client.get(f"/tenants/{alpha}/report").get_data(as_text=True)
    assert "A quiet month overall." in page and "Book a licence review" in page and "Waiting on you" in page
    assert "Client is buying P1" in page and "Chase Dave internally" in page and "Review and annotate" in page
    client.post("/logout")

    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    # Clients don't read the month's work in progress until it's approved.
    page = client.get(f"/tenants/{alpha}/report").get_data(as_text=True)
    assert "A quiet month overall." not in page and "Client is buying P1" not in page
    assert "Book a licence review" not in page
    month_end.update_review(db, alpha, PERIOD, "admin", state="approved")
    page = client.get(f"/tenants/{alpha}/report").get_data(as_text=True)
    assert "A quiet month overall." in page and "Client is buying P1" in page
    assert "Chase Dave internally" not in page and "Review and annotate" not in page
    # The PDF never has staff-only notes, even for staff.
    ctx = month_end.report_context(db, alpha, PERIOD, staff=False)["month_end"]
    assert [n["body"] for n in ctx["notes"][rule.key]] == ["Client is buying P1"]


def test_approve_freezes_sends_and_reissue_increments_version(client, db, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    pdf_html = []
    monkeypatch.setattr(delivery.export, "to_pdf", lambda html, base: pdf_html.append(html) or b"%PDF-1.7 frozen")
    _admin(client, db)
    client.post(
        "/admin/email",
        data={
            "mail_transport": "smtp",
            "smtp_host": "smtp.example.com",
            "smtp_port": "587",
            "smtp_security": "starttls",
            "smtp_user": "r@msp.com",
            "smtp_password": "x",
            "smtp_from": "r@msp.com",
        },
    )
    delivery.add_contact(db, alpha, "IT", "it@alpha.com")
    _plan(client, [alpha], include_xlsx="")
    client.post(f"/reporting/{PERIOD}/{alpha}/commentary", data={"commentary": "Signed off."})
    rule = month_end.monthly_review.build(db, store.get_tenant(db, alpha)).failing[0]
    for body, visibility in (("Visible to the client", "client"), ("Internal only", "staff")):
        client.post(
            f"/tenants/{alpha}/finding-notes", data={"rule_key": rule.key, "body": body, "visibility": visibility}
        )
    client.post(f"/reporting/{PERIOD}/approve", data={"tenants": [str(alpha)]})
    assert pdf_html == [] and sends() == []  # the request only queues it
    assert work(client) == 1
    (html,) = pdf_html  # the PDF has the commentary and client notes, never staff-only ones
    assert "Signed off." in html and "Visible to the client" in html and "Internal only" not in html
    ((_, _, msg, _),) = sends()
    assert msg["Subject"].startswith("Alpha Ltd report for ")
    rows = db.execute("SELECT * FROM issued_reports").fetchall()
    attached = {p.get_filename(): p.get_payload(decode=True) for p in msg.iter_attachments()}
    assert {r["filename"]: bytes(r["data"]) for r in rows} == attached
    assert all(
        r["trigger"] == "approval"
        and r["approved_by"] == "admin"
        and r["version"] == 1
        and r["period"] == PERIOD
        and r["recipients"] == "it@alpha.com"
        for r in rows
    )
    r = month_end.review(db, alpha, PERIOD)
    assert r["state"] == "sent" and r["approved_by"] == "admin" and r["sent_at"]
    schedule = db.execute("SELECT * FROM email_schedules").fetchone()
    assert schedule["last_period"] == PERIOD and schedule["last_status"] == "sent"

    # Approving again needs a reason, and makes version 2.
    client.post(f"/reporting/{PERIOD}/{alpha}/approve", data={})
    assert work(client) == 0 and len(sends()) == 1
    client.post(f"/reporting/{PERIOD}/{alpha}/approve", data={"reason": "Corrected the licence figures"})
    assert work(client) == 1 and len(sends()) == 2
    latest = month_end.frozen_files(db, alpha, PERIOD)
    assert [f["version"] for f in latest] == [2] * len(latest)
    assert latest[0]["reason"] == "Corrected the licence figures"
    actions = [a[0] for a in db.execute("SELECT action FROM audit_log WHERE action LIKE 'report_%' ORDER BY id")]
    assert "report_approved" in actions and "report_reissued" in actions and "report_sent" in actions
    assert "Version 2" in client.get(f"/reporting/{PERIOD}/{alpha}").get_data(as_text=True)


def test_portal_plan_publishes_without_email(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    _admin(client, db)
    delivery.add_contact(db, alpha, "IT", "it@alpha.com")
    _plan(client, [alpha], mode="auto", delivery_="portal", include_xlsx="")
    assert db.execute("SELECT on_approval FROM email_schedules").fetchone()[0] == 1
    client.post(f"/reporting/{PERIOD}/{alpha}/approve")
    work(client)
    assert sends() == []
    assert month_end.state_of(db, alpha, PERIOD) == "sent"
    rows = db.execute("SELECT * FROM issued_reports").fetchall()
    assert rows and all(r["recipients"] == "" for r in rows)
    make_user(db, "client", role="client", tenants=[alpha])
    client.post("/logout")
    login(client, "client")
    assert rows[0]["filename"] in client.get(f"/tenants/{alpha}/report-history").get_data(as_text=True)


def test_roles_and_tenant_isolation(client, db, two_tenants):
    alpha, bravo = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    _plan(client, [alpha, bravo])
    client.post("/logout")

    make_user(db, "analyst", role="analyst", tenants=[alpha])
    login(client, "analyst")
    assert client.get("/reporting/plans").status_code == 403
    assert client.post(f"/reporting/{PERIOD}/{alpha}/approve").status_code == 403
    assert client.post(f"/reporting/{PERIOD}/approve", data={"tenants": [str(alpha)]}).status_code == 403
    page = client.get(f"/reporting/{PERIOD}").get_data(as_text=True)
    assert "Alpha Ltd" in page and "Bravo Inc" not in page
    assert client.get(f"/reporting/{PERIOD}/{bravo}").status_code == 404
    assert client.post(f"/reporting/{PERIOD}/{bravo}/commentary", data={"commentary": "x"}).status_code == 404
    assert (
        client.post(
            f"/tenants/{bravo}/finding-notes", data={"rule_key": "mfa", "body": "x", "visibility": "client"}
        ).status_code
        == 404
    )
    bravo_action = month_end.add_action(db, bravo, "Theirs", "", "admin")
    assert client.post(f"/report-actions/{bravo_action}", data={"op": "delete"}).status_code == 404
    client.post("/logout")

    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    for url in (f"/reporting/{PERIOD}", f"/reporting/{PERIOD}/{alpha}", "/reporting/plans"):
        assert client.get(url).status_code == 403
    assert client.post(f"/reporting/{PERIOD}/{alpha}/commentary", data={"commentary": "x"}).status_code == 403
    assert (
        client.post(
            f"/tenants/{alpha}/finding-notes", data={"rule_key": "mfa", "body": "x", "visibility": "client"}
        ).status_code
        == 403
    )


def test_deleting_a_tenant_deletes_its_month_end_rows(db, two_tenants):
    alpha, bravo = two_tenants
    plan_id = month_end.save_plan(
        db,
        {
            "name": "P",
            "reports": ["review"],
            "include_xlsx": 0,
            "day_of_month": 1,
            "hour": 8,
            "timezone": "",
            "subject": "",
            "message": "",
            "mode": "approval",
            "delivery": "email",
            "bcc_team": 0,
        },
        "admin",
    )
    month_end.apply_plan(db, month_end.get_plan(db, plan_id), [alpha, bravo], "admin", "UTC")
    for t in (alpha, bravo):
        month_end.update_review(db, t, PERIOD, "admin", commentary="x")
        month_end.add_action(db, t, "Do it", "", "admin")
        db.execute(
            "INSERT INTO finding_notes (tenant_id, rule_key, body, visibility, created_at)"
            " VALUES (?, 'mfa', 'n', 'client', '2026-10-01T00:00:00Z')",
            (t,),
        )
    lifecycle.delete_tenant(db, store.get_tenant(db, alpha), "admin")
    for table in ("report_plan_tenants", "report_reviews", "report_actions", "finding_notes"):
        assert [r[0] for r in db.execute(f"SELECT tenant_id FROM {table}")] == [bravo]


def _auto_plan(db, tenant_id, mode="auto", delivery_="email", now=None):
    plan_id = month_end.save_plan(
        db,
        {
            "name": "P",
            "reports": ["review"],
            "include_xlsx": 0,
            "day_of_month": 1,
            "hour": 0,
            "timezone": "UTC",
            "subject": "",
            "message": "",
            "mode": mode,
            "delivery": delivery_,
            "bcc_team": 0,
        },
        "admin",
    )
    month_end.apply_plan(db, month_end.get_plan(db, plan_id), [tenant_id], "admin", "UTC", now=now)
    return db.execute("SELECT * FROM email_schedules WHERE tenant_id = ?", (tenant_id,)).fetchone()


def test_commenting_on_a_month_the_timer_sent_keeps_it_sent(db, two_tenants):
    alpha, bravo = two_tenants
    schedule = _auto_plan(db, alpha)
    pdf = month_end.issued.IssuedFile("review", "Monthly review", "review.pdf", "application/pdf", b"%PDF")
    month_end.issued.archive(
        db, alpha, [pdf], period="2026-09", trigger="schedule", user=None, schedule_id=schedule["id"]
    )
    # Another schedule's report for the month doesn't make the plan's month Sent.
    month_end.issued.archive(db, alpha, [pdf], period="2026-08", trigger="manual", user="admin")
    assert month_end.state_of(db, alpha, "2026-08") == "draft"

    assert month_end.state_of(db, alpha, "2026-09") == "sent"
    month_end.update_review(db, alpha, "2026-09", "admin", commentary="Late note")
    month_end.update_review(db, alpha, "2026-09", "admin", reviewer="admin")
    month_end.start_review(db, alpha, "2026-09", "admin")
    assert month_end.review(db, alpha, "2026-09")["state"] == "sent"
    # A month nothing went out for still starts as a draft.
    month_end.update_review(db, alpha, "2026-10", "admin", commentary="x")
    assert month_end.review(db, alpha, "2026-10")["state"] == "draft"
    month_end.update_review(db, bravo, "2026-09", "admin", commentary="x")
    assert month_end.review(db, bravo, "2026-09")["state"] == "draft"


def test_issuing_an_earlier_month_never_moves_the_schedule_back(app, db, two_tenants, vault, cfg, monkeypatch):
    alpha, bravo = two_tenants
    monkeypatch.setattr(delivery.export, "to_pdf", lambda html, base: b"%PDF-1.7")
    seed_tenant(db, alpha)
    seed_tenant(db, bravo)
    _smtp_ready(db)
    delivery.add_contact(db, alpha, "", "it@alpha.com")
    oct5 = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    email_schedule = _auto_plan(db, alpha, now=oct5)
    portal_schedule = _auto_plan(db, bravo, delivery_="portal", now=oct5)
    assert email_schedule["last_period"] == portal_schedule["last_period"] == "2026-10"
    for tenant_id in (alpha, bravo):
        tenant = store.get_tenant(db, tenant_id)
        month_end.freeze(app, db, tenant, "2026-09", "admin", now=oct5)
        assert month_end.deliver(app, db, vault, tenant, "2026-09", "admin", now=oct5) is None
    assert len(sends()) == 1
    assert [r[0] for r in db.execute("SELECT last_period FROM email_schedules ORDER BY tenant_id")] == [
        "2026-10",
        "2026-10",
    ]
    db.execute("UPDATE runs SET finished_at = ?", (oct5.strftime("%Y-%m-%dT%H:%M:%SZ"),))
    assert delivery.tick(app, db, vault, cfg, oct5) == 0  # October isn't sent again

    # A month approved ahead of its send time counts as done when the slot comes round.
    db.execute("UPDATE email_schedules SET day_of_month = 28")
    month_end.freeze(app, db, store.get_tenant(db, alpha), "2026-10", "admin", now=oct5)
    month_end.deliver(app, db, vault, store.get_tenant(db, alpha), "2026-10", "admin", now=oct5)
    assert len(sends()) == 2
    assert delivery.tick(app, db, vault, cfg, datetime(2026, 10, 28, 1, 0, tzinfo=UTC)) == 0
    assert delivery.period_done("2026-11", "2026-10") and not delivery.period_done("2026-09", "2026-10")
    assert not delivery.period_done("2026-W41", "2026-10") and not delivery.period_done(None, "2026-10")


def test_switching_a_plan_to_automatic_does_not_email_an_unapproved_month(
    app, db, two_tenants, vault, cfg, monkeypatch
):
    alpha, _ = two_tenants
    monkeypatch.setattr(delivery.export, "to_pdf", lambda html, base: b"%PDF-1.7")
    seed_tenant(db, alpha)
    _smtp_ready(db)
    delivery.add_contact(db, alpha, "", "it@alpha.com")
    schedule = _auto_plan(db, alpha, mode="approval", now=datetime(2026, 9, 15, tzinfo=UTC))
    assert schedule["last_period"] == "2026-09"
    oct5 = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)  # October's month-end is waiting for approval
    plan = month_end.get_plan(db, schedule["plan_id"])
    db.execute("UPDATE report_plans SET mode = 'auto' WHERE id = ?", (plan["id"],))
    month_end.apply_plan(db, month_end.get_plan(db, plan["id"]), [alpha], "admin", "UTC", now=oct5)
    s = db.execute("SELECT * FROM email_schedules").fetchone()
    assert s["on_approval"] == 0 and s["last_period"] == "2026-10"
    db.execute("UPDATE runs SET finished_at = ?", (oct5.strftime("%Y-%m-%dT%H:%M:%SZ"),))
    assert delivery.tick(app, db, vault, cfg, oct5) == 0 and sends() == []
