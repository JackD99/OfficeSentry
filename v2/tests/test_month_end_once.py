"""A month goes to a client once: the timer, Retry, approval and rebuilt plans never send it a second time."""

from datetime import UTC, datetime, timedelta

from officesentry import delivery, month_end, tasks
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


def _automatic(client, db, tenant_id, **extra) -> dict:
    """An automatic plan for a client with a contact and data, signed in as an admin. Returns its schedule."""
    _admin(client, db)
    _smtp_ready(db)
    _collected(db, tenant_id)
    db.execute("UPDATE tenants SET connection_id = NULL")  # no refresh before sending: nothing to sign in to
    delivery.add_contact(db, tenant_id, "IT", "it@alpha.com")
    assert _plan(client, [tenant_id], mode="auto", **extra).status_code == 302
    return db.execute("SELECT * FROM email_schedules WHERE tenant_id = ?", (tenant_id,)).fetchone()


def test_a_month_the_timer_sent_while_someone_reviewed_it_isnt_approved_again(client, app, db, two_tenants, vault, cfg):
    alpha, _ = two_tenants
    _automatic(client, db, alpha, day_of_month="20")
    db.execute("UPDATE email_schedules SET last_period = '2026-09'")
    client.post(f"/reporting/2026-10/{alpha}/commentary", data={"commentary": "Good month."})  # 12 October
    assert month_end.state_of(db, alpha, "2026-10") == "in_review"
    assert delivery.tick(app, db, vault, cfg, datetime(2026, 10, 20, 8, 0, tzinfo=UTC)) == 1
    assert month_end.state_of(db, alpha, "2026-10") == "sent"
    page = client.get("/reporting/2026-10").get_data(as_text=True)
    assert f'name="tenants" value="{alpha}"' not in page
    client.post("/reporting/2026-10/approve", data={"tenants": [str(alpha)]})
    work(client)
    assert len(sends()) == 1


def test_retry_after_the_period_went_out_doesnt_send_it_again(client, app, db, two_tenants, vault, cfg):
    alpha, _ = two_tenants
    _admin(client, db)
    _smtp_ready(db)
    _collected(db, alpha)
    db.execute("UPDATE tenants SET connection_id = NULL")
    sid = _schedule(db, alpha, [delivery.add_contact(db, alpha, "", "a@x.com")], hour=8)
    now = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
    FakeSMTP.fail_with = OSError("connection refused")
    assert delivery.tick(app, db, vault, cfg, now) == 1
    FakeSMTP.fail_with = None
    assert delivery.tick(app, db, vault, cfg, now + timedelta(minutes=5)) == 1  # the automatic retry
    assert len(sends()) == 1 and _row(db, "email_schedules", sid)["last_period"] == "2026-10"
    failed = db.execute("SELECT * FROM email_deliveries WHERE status = 'failed'").fetchone()

    error = delivery.retry_delivery(app, db, vault, failed, "admin")
    assert error.startswith("It went out on ") and error.endswith(" after this attempt failed, so it isn't sent again.")
    assert len(sends()) == 1
    page = client.get(f"/tenants/{alpha}/schedules").get_data(as_text=True)
    assert f"/deliveries/{failed['id']}/retry" not in page

    # A row from an earlier period would send this period's report.
    later = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
    assert delivery.retry_refused(db, _row(db, "email_schedules", sid), failed, "UTC", later).startswith(
        "This email was for October 2026 and the schedule has moved on to November 2026"
    )


def test_retry_on_a_failed_approval_email_sends_the_approved_files_from_month_end(
    client, app, db, two_tenants, vault, cfg
):
    alpha, _ = two_tenants
    s = _automatic(client, db, alpha)
    period = month_end.current_period("UTC")
    FakeSMTP.fail_with = OSError("down")
    client.post(f"/reporting/{period}/{alpha}/approve")
    work(client)
    FakeSMTP.fail_with = None
    assert month_end.state_of(db, alpha, period) == "approved"
    failed = db.execute("SELECT * FROM email_deliveries WHERE status = 'failed'").fetchone()
    assert "send it again from there" in delivery.retry_delivery(app, db, vault, failed, "admin")
    # Nor does the timer email a live copy of the month in the meantime.
    db.execute("UPDATE email_schedules SET last_period = NULL WHERE id = ?", (s["id"],))
    assert not delivery._due(db, _row(db, "email_schedules", s["id"]), cfg, datetime.now(UTC))
    assert len(sends()) == 0
    client.post(f"/reporting/{period}/{alpha}/send")
    work(client)
    assert len(sends()) == 1
    assert [r["trigger"] for r in db.execute("SELECT trigger FROM issued_reports")] == ["approval", "approval"]


def test_a_plan_made_again_remembers_the_months_it_sent(client, app, db, two_tenants, vault):
    alpha, _ = two_tenants
    s = _automatic(client, db, alpha)
    period = month_end.current_period("UTC")
    db.execute("UPDATE email_schedules SET last_period = NULL")
    assert delivery.send_schedule(app, db, vault, _row(db, "email_schedules", s["id"]), trigger="schedule") is None
    assert month_end.state_of(db, alpha, period) == "sent"
    plan_id = db.execute("SELECT id FROM report_plans").fetchone()[0]
    client.post(f"/reporting/plans/{plan_id}/delete")
    assert _plan(client, [alpha], mode="approval").status_code == 302
    assert month_end.state_of(db, alpha, period) == "sent"
    client.post(f"/reporting/{period}/approve", data={"tenants": [str(alpha)]})
    work(client)
    assert len(sends()) == 1


def test_resuming_or_retiming_a_schedule_never_unsends_a_month_approved_early(client, app, db, two_tenants, vault, cfg):
    alpha, _ = two_tenants
    s = _automatic(client, db, alpha)
    nxt = month_end.shift_period(month_end.current_period("UTC"), 1)
    client.post(f"/reporting/{nxt}/{alpha}/approve")  # next month approved and sent early
    work(client)
    assert len(sends()) == 1 and _row(db, "email_schedules", s["id"])["last_period"] == nxt
    client.post(f"/schedules/{s['id']}/toggle")
    client.post(f"/schedules/{s['id']}/toggle")
    assert _row(db, "email_schedules", s["id"])["last_period"] == nxt
    year, month = int(nxt[:4]), int(nxt[5:])
    delivery.tick(app, db, vault, cfg, datetime(year, month, 1, 8, 0, tzinfo=UTC))
    assert len(sends()) == 1


def test_approving_waits_while_the_timer_is_sending_the_month(client, app, db, two_tenants, vault, cfg):
    alpha, _ = two_tenants
    s = _automatic(client, db, alpha)
    period = month_end.current_period("UTC")
    tasks.enqueue(db, "send_schedule", key=delivery.send_key(s, period), payload={"schedule_id": s["id"]})
    db.execute("UPDATE tasks SET status = 'running' WHERE kind = 'send_schedule'")  # the timer's, mid-send
    client.post(f"/reporting/{period}/{alpha}/approve")
    work(client)
    assert month_end.approval_task(db, alpha, period)["error"] == delivery.BEING_SENT
    assert len(sends()) == 0 and month_end.state_of(db, alpha, period) == "draft"
