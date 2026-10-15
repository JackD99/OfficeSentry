"""What month-end and scheduled emails send: never a blank report or an empty email, never to a paused client,
and a plan's settings reach every client on it."""

from datetime import UTC, datetime

from officesentry import delivery, lifecycle, month_end, store
from officesentry.issued import IssuedFile, archive
from officesentry.reports import designs, monthly_review
from tests.test_email import (  # noqa: F401 (autouse fixture)
    FakeSMTP,
    _collected,
    _row,
    _schedule,
    _smtp_ready,
    fake_smtp,
    sends,
)
from tests.test_month_end import _admin, _plan, work


def _ready(client, db, tenant_id, **plan) -> None:
    """An admin, mail set up and a client with a contact on a plan that waits for approval."""
    _admin(client, db)
    _smtp_ready(db)
    delivery.add_contact(db, tenant_id, "IT", "it@alpha.com")
    assert _plan(client, [tenant_id], **plan).status_code == 302


def test_a_client_nothing_was_collected_for_isnt_approved(client, db, two_tenants):
    alpha, _ = two_tenants
    _ready(client, db, alpha)
    period = month_end.current_period("UTC")
    queue = client.get(f"/reporting/{period}").get_data(as_text=True)
    assert "No data collected yet" in queue
    assert f'name="tenants" value="{alpha}"' not in queue  # no tick box to approve it with

    review = client.get(f"/reporting/{period}/{alpha}").get_data(as_text=True)
    assert "Nothing has been collected for Alpha Ltd yet" in review and 'id="approve-form"' not in review
    page = client.post(f"/reporting/{period}/{alpha}/approve", follow_redirects=True).get_data(as_text=True)
    assert "Nothing has been collected for Alpha Ltd yet" in page
    page = client.post(f"/reporting/{period}/approve", data={"tenants": [str(alpha)]}, follow_redirects=True)
    assert "Skipped 1 with no report to send" in page.get_data(as_text=True)
    assert month_end.approval_task(db, alpha, period) is None

    # Queued before the client's last data went (or by an older page): the approval refuses, nothing is sent.
    month_end.queue_approval(db, store.get_tenant(db, alpha), period, "admin")
    work(client)
    assert month_end.approval_task(db, alpha, period)["error"].startswith("Nothing has been collected")
    assert not sends() and month_end.state_of(db, alpha, period) == "draft"


def test_a_plan_whose_only_report_was_deleted_says_so_and_sends_nothing(client, db, two_tenants):
    alpha, _ = two_tenants
    did = designs.create(db, "Board pack", "admin", designs.STARTERS["executive"][2])
    _ready(client, db, alpha, reports=[designs.key_of(did)])
    _collected(db, alpha)
    period = month_end.current_period("UTC")
    design = designs.document(designs.get(db, did))
    month_end.leave_out(db, alpha, period, designs.block_item(did, design["blocks"][0]["id"]), "admin")

    page = client.post(f"/reporting/designs/{did}/delete", follow_redirects=True).get_data(as_text=True)
    assert "“Monthly” has no reports left, so nothing is approved or sent from it" in page
    # What was left out of it goes with it: the next design gets the same id and its blocks would be left out.
    assert not db.execute("SELECT 1 FROM report_hidden WHERE item LIKE ?", (f"block:{did}:%",)).fetchone()
    assert "No reports" in client.get("/reporting/plans").get_data(as_text=True)

    assert "No report to send" in client.get(f"/reporting/{period}").get_data(as_text=True)
    page = client.post(f"/reporting/{period}/{alpha}/approve", follow_redirects=True).get_data(as_text=True)
    assert "The plan “Monthly” has no reports to send. Choose one on the plan first." in page
    month_end.queue_approval(db, store.get_tenant(db, alpha), period, "admin")
    work(client)
    assert month_end.approval_task(db, alpha, period)["error"].startswith("The plan “Monthly” has no reports")
    assert not sends() and month_end.state_of(db, alpha, period) == "draft"


def test_a_pdf_only_plan_on_a_server_without_pdfs_isnt_approved(client, db, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    _ready(client, db, alpha, reports=[monthly_review.SUMMARY_KEY])
    _collected(db, alpha)
    monkeypatch.setattr(delivery.export, "pdf_available", lambda: False)
    period = month_end.current_period("UTC")
    page = client.post(f"/reporting/{period}/{alpha}/approve", follow_redirects=True).get_data(as_text=True)
    assert "only sends PDFs, and this server can&#39;t make them" in page
    assert month_end.approval_task(db, alpha, period) is None


def test_saving_a_plan_changes_its_archived_clients_too(client, db, two_tenants):
    alpha, bravo = two_tenants
    _admin(client, db)
    assert _plan(client, [alpha, bravo], mode="auto").status_code == 302
    plan_id = db.execute("SELECT id FROM report_plans").fetchone()[0]
    lifecycle.archive(db, store.get_tenant(db, bravo), "admin")

    # The plan now waits for approval. The form doesn't list the archived client, but it stays on the plan.
    assert _plan_save(client, plan_id, [alpha], mode="approval").status_code == 302
    lifecycle.restore(db, store.get_tenant(db, bravo), "admin")
    s = db.execute("SELECT * FROM email_schedules WHERE tenant_id = ?", (bravo,)).fetchone()
    assert s["on_approval"] == 1 and s["plan_id"] == plan_id
    assert month_end.plan_for(db, bravo)[0]["id"] == plan_id


def _plan_save(client, plan_id, tenants, **extra):
    data = {
        "name": "Monthly",
        "reports": ["review"],
        "include_xlsx": "1",
        "day_of_month": "1",
        "hour": "8",
        "timezone": "UTC",
        "subject": "{client} report for {month}",
        "message": "Hello {client}",
        "mode": "approval",
        "delivery": "email",
        "tenants": [str(t) for t in tenants],
    }
    data.update(extra)
    return client.post(f"/reporting/plans/{plan_id}", data=data)


def test_nothing_is_emailed_to_a_paused_client(client, app, db, two_tenants, vault, cfg):
    alpha, bravo = two_tenants
    _admin(client, db)
    _smtp_ready(db)
    for t in (alpha, bravo):
        _collected(db, t)
    db.execute("UPDATE tenants SET connection_id = NULL")
    sid = _schedule(db, alpha, [delivery.add_contact(db, alpha, "", "a@x.com")], last_period="2026-09")
    FakeSMTP.fail_with = OSError("down")
    assert delivery.tick(app, db, vault, cfg, datetime(2026, 10, 1, 8, 0, tzinfo=UTC)) == 1
    FakeSMTP.fail_with = None
    failed = db.execute("SELECT * FROM email_deliveries WHERE status = 'failed'").fetchone()
    lifecycle.pause(db, store.get_tenant(db, alpha), "admin")

    page = client.get(f"/tenants/{alpha}/schedules").get_data(as_text=True)
    assert "Client paused" in page and "Due now" not in page and f"/schedules/{sid}/send" not in page
    assert "Due now" not in client.get("/emails").get_data(as_text=True)
    page = client.post(f"/schedules/{sid}/send", follow_redirects=True).get_data(as_text=True)
    assert "Alpha Ltd is paused, so nothing is sent to it. Resume it first." in page
    page = client.post(f"/deliveries/{failed['id']}/retry", follow_redirects=True).get_data(as_text=True)
    assert "Alpha Ltd is paused, so nothing is sent to it." in page
    assert delivery.send_schedule(app, db, vault, _row(db, "email_schedules", sid)).startswith("Alpha Ltd is paused")
    assert not sends()

    # Resuming doesn't send what came due while it was paused; the next one goes as usual.
    lifecycle.resume(db, store.get_tenant(db, alpha), "admin", "UTC")
    assert delivery.tick(app, db, vault, cfg, datetime(2026, 10, 27, 15, 0, tzinfo=UTC)) == 0
    assert delivery.tick(app, db, vault, cfg, datetime(2026, 11, 1, 8, 0, tzinfo=UTC)) == 1
    assert len(sends()) == 1


def test_send_again_waits_for_a_paused_client(client, db, two_tenants):
    alpha, _ = two_tenants
    _ready(client, db, alpha)
    _collected(db, alpha)
    period = month_end.current_period("UTC")
    FakeSMTP.fail_with = OSError("down")
    client.post(f"/reporting/{period}/{alpha}/approve")
    work(client)
    FakeSMTP.fail_with = None
    assert month_end.state_of(db, alpha, period) == "approved"
    lifecycle.pause(db, store.get_tenant(db, alpha), "admin")

    page = client.post(f"/reporting/{period}/{alpha}/send", follow_redirects=True).get_data(as_text=True)
    assert "Alpha Ltd is paused, so nothing is sent to it. Resume it first." in page
    # Paused after Send again was queued: the task stops before sending.
    month_end.queue_approval(db, store.get_tenant(db, alpha), period, "admin", send_only=True)
    work(client)
    assert month_end.approval_task(db, alpha, period)["error"].startswith("Alpha Ltd is paused")
    assert not sends() and month_end.state_of(db, alpha, period) == "approved"


def test_a_plan_email_is_changed_on_its_plan(client, db, two_tenants):
    alpha, _ = two_tenants
    _ready(client, db, alpha)
    sid = db.execute("SELECT id FROM email_schedules WHERE tenant_id = ?", (alpha,)).fetchone()[0]
    page = client.get(f"/tenants/{alpha}/schedules").get_data(as_text=True)
    assert f"/schedules/{sid}/edit" not in page and "Edit plan" in page

    for response in (client.get(f"/schedules/{sid}/edit"), client.post(f"/schedules/{sid}/delete")):
        assert response.status_code == 302
    page = client.post(f"/schedules/{sid}/delete", follow_redirects=True).get_data(as_text=True)
    assert "This email comes from the report plan “Monthly”" in page
    assert _row(db, "email_schedules", sid) is not None


def test_new_critical_since_the_last_report_counts_weekly_reports(db, two_tenants):
    alpha, _ = two_tenants
    f = IssuedFile("review", "Monthly review", "r.pdf", "application/pdf", b"%PDF")
    archive(db, alpha, [f], period="2025-W50", trigger="schedule", user=None, now="2025-12-08T08:00:00Z")
    assert month_end._last_report_at(db, alpha, "2026-10") == "2025-12-08T08:00:00Z"
    archive(db, alpha, [f], period="2026-W40", trigger="schedule", user=None, now="2026-09-28T08:00:00Z")
    archive(db, alpha, [f], period="2026-Q3", trigger="schedule", user=None, now="2026-07-01T08:00:00Z")
    assert month_end._last_report_at(db, alpha, "2026-10") == "2026-09-28T08:00:00Z"
    # September's report approved late, in October, still counts; October's own doesn't.
    archive(db, alpha, [f], period="2026-09", trigger="approval", user="admin", now="2026-10-03T09:00:00Z")
    archive(db, alpha, [f], period="2026-10", trigger="approval", user="admin", now="2026-10-04T09:00:00Z")
    assert month_end._last_report_at(db, alpha, "2026-10") == "2026-10-03T09:00:00Z"
