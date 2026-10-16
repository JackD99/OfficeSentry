"""Background tasks: the queue, leases, and month-end work done off the request path, exactly once."""

import re
from datetime import UTC, datetime, timedelta

import pytest

from officesentry import delivery, month_end, store, tasks, worker
from officesentry.reports.data_reports import DataReport, Section
from officesentry.web import printing
from tests.conftest import login, make_user
from tests.seed import seed_tenant
from tests.test_email import _schedule, _smtp_ready, fake_smtp, sends  # noqa: F401 (autouse fixture)
from tests.test_month_end import PERIOD, _plan, work


@pytest.fixture(autouse=True)
def _handlers():
    worker._load_modules()  # registers the approve and send_schedule handlers, as the worker does


@pytest.fixture
def echo():
    calls = []

    @tasks.handler("echo")
    def run(ctx):
        calls.append(ctx.payload)
        if ctx.payload.get("fail"):
            raise tasks.TaskFailed("It didn't work")
        if ctx.payload.get("bug"):
            raise KeyError("oops")
        return {"ok": True}

    yield calls
    tasks.HANDLERS.pop("echo", None)


def test_queue_dedupes_by_key_and_claims_by_priority(app, db, cfg, vault, echo):
    first = tasks.enqueue(db, "echo", key="k1", payload={"n": 1})
    assert tasks.enqueue(db, "echo", key="k1", payload={"n": 2}) == first  # one pending task per key
    urgent = tasks.enqueue(db, "echo", payload={"n": 3}, priority=tasks.PRIORITY_WAITING)
    assert tasks.claim_next(db)["id"] == urgent
    task = tasks.claim_next(db)
    assert task["id"] == first and task["attempts"] == 1 and task["lease_until"]
    assert tasks.claim_next(db) is None
    assert tasks.run(app, db, vault, cfg, task) == "succeeded"
    assert tasks.latest(db, "k1")["result"] == '{"ok": true}'
    # Finished, so the key is free again.
    assert tasks.enqueue(db, "echo", key="k1") != first
    with pytest.raises(ValueError):
        tasks.enqueue(db, "nobody-handles-this")


def test_failures_are_recorded_not_raised(app, db, cfg, vault, echo):
    failed = tasks.enqueue(db, "echo", payload={"fail": True})
    crashed = tasks.enqueue(db, "echo", payload={"bug": True})
    assert tasks.drain(app, db, vault, cfg) == 2
    rows = {r["id"]: r for r in db.execute("SELECT * FROM tasks")}
    assert (rows[failed]["status"], rows[failed]["error"]) == ("failed", "It didn't work")
    assert (rows[crashed]["status"], rows[crashed]["error"]) == ("failed", "Something went wrong: KeyError")
    assert not db.in_transaction


def test_a_dead_workers_task_is_queued_again_then_given_up(db, echo):
    task_id = tasks.enqueue(db, "echo")
    tasks.claim_next(db)
    assert tasks.expire_leases(db) == (0, 0)  # the lease is current
    later = datetime.now(UTC) + store.LEASE + timedelta(seconds=1)
    assert tasks.expire_leases(db, later, held=[task_id]) == (0, 0)  # this process still runs it
    tasks.renew(db, [task_id], later)
    assert tasks.expire_leases(db, later) == (0, 0)  # renewed by the heartbeat
    much_later = later + store.LEASE + timedelta(seconds=1)
    for attempt in range(2, tasks.MAX_ATTEMPTS + 1):
        assert tasks.expire_leases(db, much_later) == (1, 0)
        assert tasks.claim_next(db)["attempts"] == attempt
    assert tasks.expire_leases(db, much_later) == (0, 1)
    row = db.execute("SELECT * FROM tasks").fetchone()
    assert row["status"] == "failed" and "stopped" in row["error"]
    db.execute("UPDATE tasks SET finished_at = '2020-01-01T00:00:00Z'")
    assert tasks.prune(db) == 1


def test_heartbeat_renews_what_the_worker_holds(db, two_tenants, echo):
    store.enqueue_run(db, two_tenants[0], "users")
    run = store.claim_next_run(db)
    task_id = tasks.enqueue(db, "echo")
    tasks.claim_next(db)
    db.execute("UPDATE runs SET lease_until = '2000-01-01T00:00:00Z'")
    db.execute("UPDATE tasks SET lease_until = '2000-01-01T00:00:00Z'")
    live = worker.Liveness()
    live.start_run(run["id"])
    live.start_task(task_id)  # (the same thread here, so the task replaces the run in Liveness)
    assert live.held("task") == [task_id]
    worker.heartbeat_step(db, live)
    assert db.execute("SELECT lease_until FROM tasks").fetchone()[0] > store.utcnow()
    live.end_task()
    assert live.held("task") == []


def test_a_long_task_stops_the_heartbeat():
    now = [0.0]
    live = worker.Liveness(clock=lambda: now[0])
    live.start_task(9)
    now[0] += worker.TASK_STALE.total_seconds() + 1
    live.tick()
    assert "task 9" in live.problem()


# -- Month-end approval ----------------------------------------------------------------------


def _planned(client, db, monkeypatch, *tenant_ids, **plan):
    rendered = []
    monkeypatch.setattr(delivery.export, "to_pdf", lambda html, base: rendered.append(html) or b"%PDF-1.7")
    make_user(db, "admin")
    login(client, "admin")
    _smtp_ready(db)
    for tenant_id in tenant_ids:
        seed_tenant(db, tenant_id)
        delivery.add_contact(db, tenant_id, "IT", f"it@{tenant_id}.example")
    _plan(client, list(tenant_ids), include_xlsx="", **plan)
    return rendered


def test_bulk_approval_returns_at_once_and_shows_progress(client, db, two_tenants, monkeypatch):
    alpha, bravo = two_tenants
    rendered = _planned(client, db, monkeypatch, alpha, bravo)
    resp = client.post(f"/reporting/{PERIOD}/approve", data={"tenants": [str(alpha), str(bravo)]})
    assert resp.status_code == 302 and "batch=" in resp.location
    assert rendered == [] and sends() == []  # nothing rendered or sent in the request
    page = client.get(resp.location).get_data(as_text=True)
    assert "Approving 2 reports" in page and "0 of 2 done" in page and "Waiting to approve" in page
    batch = month_end.latest_batch(db, PERIOD)
    progress = client.get(f"/reporting/{PERIOD}/progress?batch={batch}").get_json()
    assert progress == {"total": 2, "done": 0, "succeeded": 0, "failed": 0, "pending": 2, "finished": False}
    # Asking again while they wait queues nothing more.
    client.post(f"/reporting/{PERIOD}/approve", data={"tenants": [str(alpha)]})
    assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2

    assert work(client) == 2
    assert len(sends()) == 2 and len(rendered) == 2
    assert client.get(f"/reporting/{PERIOD}/progress?batch={batch}").get_json()["finished"] is True
    page = client.get(resp.location).get_data(as_text=True)
    assert "All 2 reports went out" in page
    assert "went out" not in client.get(f"/reporting/{PERIOD}").get_data(as_text=True)
    assert [month_end.state_of(db, t, PERIOD) for t in (alpha, bravo)] == ["sent", "sent"]
    assert client.get(f"/reporting/{PERIOD}/progress?batch=approve:2020-01:x").status_code == 404


def test_progress_only_counts_clients_the_viewer_sees(client, db, two_tenants, monkeypatch):
    alpha, bravo = two_tenants
    _planned(client, db, monkeypatch, alpha, bravo)
    client.post(f"/reporting/{PERIOD}/approve", data={"tenants": [str(alpha), str(bravo)]})
    batch = month_end.latest_batch(db, PERIOD)
    client.post("/logout")
    make_user(db, "analyst", role="analyst", tenants=[alpha])
    login(client, "analyst")
    assert client.get(f"/reporting/{PERIOD}/progress?batch={batch}").get_json()["total"] == 1
    assert client.get(f"/reporting/{PERIOD}/progress?tenant={bravo}").status_code == 404
    assert client.get(f"/reporting/{PERIOD}/progress?tenant={alpha}").get_json()["pending"] == 1


def test_one_approval_per_client_and_month(client, db, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    _planned(client, db, monkeypatch, alpha)
    client.post(f"/reporting/{PERIOD}/{alpha}/approve")
    page = client.get(f"/reporting/{PERIOD}/{alpha}").get_data(as_text=True)
    assert "Waiting for the background worker" in page and "Approve and send</button>" not in page
    # A double click, or a second admin, while it waits.
    client.post(f"/reporting/{PERIOD}/{alpha}/approve")
    client.post(f"/reporting/{PERIOD}/approve", data={"tenants": [str(alpha)]})
    assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert work(client) == 1 and len(sends()) == 1
    # Approved now: another approval needs a reason (a re-issue), and a stale queued one does nothing.
    month_end.queue_approval(db, store.get_tenant(db, alpha), PERIOD, "admin")
    assert work(client) == 1 and len(sends()) == 1
    assert len(month_end.frozen_files(db, alpha, PERIOD)) == 1


class WorkerDied(BaseException):
    """Stands in for the process being killed: nothing in the worker catches it."""


def test_a_worker_that_dies_between_freeze_and_send_sends_once(app, client, db, two_tenants, monkeypatch, vault, cfg):
    alpha, _ = two_tenants
    rendered = _planned(client, db, monkeypatch, alpha)
    client.post(f"/reporting/{PERIOD}/{alpha}/approve")
    real_deliver = month_end.deliver

    def die(*args, **kwargs):
        raise WorkerDied

    monkeypatch.setattr(month_end, "deliver", die)
    with pytest.raises(WorkerDied):
        work(client)
    task = db.execute("SELECT * FROM tasks").fetchone()
    assert task["status"] == "running" and task["checkpoint"] == "frozen"  # frozen and committed, not sent
    assert month_end.state_of(db, alpha, PERIOD) == "approved" and sends() == []

    # Its lease runs out, the next worker takes it back and only sends.
    monkeypatch.setattr(month_end, "deliver", real_deliver)
    later = datetime.now(UTC) + store.LEASE + timedelta(minutes=1)
    assert tasks.expire_leases(db, later) == (1, 0)
    assert work(client) == 1
    assert len(sends()) == 1 and len(rendered) == 1
    files = month_end.frozen_files(db, alpha, PERIOD)
    assert files and {f["version"] for f in files} == {1}
    assert db.execute("SELECT COUNT(*) FROM issued_reports").fetchone()[0] == len(files)
    assert month_end.state_of(db, alpha, PERIOD) == "sent"
    actions = [a[0] for a in db.execute("SELECT action FROM audit_log WHERE action LIKE 'report_%' ORDER BY id")]
    assert actions.count("report_approved") == 1 and actions.count("report_sent") == 1


def test_a_worker_that_dies_after_sending_does_not_send_again(client, db, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    _planned(client, db, monkeypatch, alpha)
    client.post(f"/reporting/{PERIOD}/{alpha}/approve")
    real_finish = tasks.finish

    def die(conn, task_id, status, **kwargs):
        raise WorkerDied

    monkeypatch.setattr(tasks, "finish", die)
    with pytest.raises(WorkerDied):
        work(client)
    assert len(sends()) == 1 and month_end.state_of(db, alpha, PERIOD) == "sent"
    monkeypatch.setattr(tasks, "finish", real_finish)
    tasks.expire_leases(db, datetime.now(UTC) + store.LEASE + timedelta(minutes=1))
    assert work(client) == 1
    assert len(sends()) == 1  # Sent was committed with the email's record, so the retry sees it


def test_a_worker_that_dies_while_rendering_renders_again(client, db, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    rendered = _planned(client, db, monkeypatch, alpha)
    client.post(f"/reporting/{PERIOD}/{alpha}/approve", data={"reason": ""})
    real = month_end.render_files
    monkeypatch.setattr(month_end, "render_files", lambda *a, **k: (_ for _ in ()).throw(WorkerDied()))
    with pytest.raises(WorkerDied):
        work(client)
    assert db.execute("SELECT COUNT(*) FROM issued_reports").fetchone()[0] == 0
    monkeypatch.setattr(month_end, "render_files", real)
    tasks.expire_leases(db, datetime.now(UTC) + store.LEASE + timedelta(minutes=1))
    work(client)
    assert len(rendered) == 1 and len(sends()) == 1
    assert {f["version"] for f in month_end.frozen_files(db, alpha, PERIOD)} == {1}


def test_a_failed_send_shows_and_can_be_sent_again(client, db, two_tenants, monkeypatch):
    from tests.test_email import FakeSMTP

    alpha, _ = two_tenants
    rendered = _planned(client, db, monkeypatch, alpha)
    client.post(f"/reporting/{PERIOD}/{alpha}/approve")
    FakeSMTP.fail_with = OSError("connection refused")
    work(client)
    page = client.get(f"/reporting/{PERIOD}/{alpha}").get_data(as_text=True)
    assert "the email didn't send" in page and "Send again" in page
    assert "Approval failed" not in client.get(f"/reporting/{PERIOD}").get_data(as_text=True)
    FakeSMTP.fail_with = None
    client.post(f"/reporting/{PERIOD}/{alpha}/send")
    assert work(client) == 1
    assert len(sends()) == 1 and len(rendered) == 1  # the frozen files, not rebuilt
    assert month_end.state_of(db, alpha, PERIOD) == "sent"


def test_a_build_failure_is_shown_on_the_queue_and_review(client, db, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    _planned(client, db, monkeypatch, alpha)
    monkeypatch.setattr(month_end, "render_files", lambda *a, **k: 1 / 0)
    client.post(f"/reporting/{PERIOD}/{alpha}/approve")
    work(client)
    assert month_end.state_of(db, alpha, PERIOD) == "draft"
    assert "Approval failed" in client.get(f"/reporting/{PERIOD}").get_data(as_text=True)
    page = client.get(f"/reporting/{PERIOD}/{alpha}").get_data(as_text=True)
    assert "The last approval didn" in page and "Couldn&#39;t build the report: ZeroDivisionError" in page


def test_paused_clients_are_not_approved(client, db, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    _planned(client, db, monkeypatch, alpha)
    client.post(f"/reporting/{PERIOD}/{alpha}/approve")
    db.execute("UPDATE tenants SET enabled = 0 WHERE id = ?", (alpha,))  # paused while it waited
    work(client)
    assert sends() == [] and month_end.state_of(db, alpha, PERIOD) == "draft"
    assert tasks.latest(db, month_end.approval_key(alpha, PERIOD))["error"] == (
        "Alpha Ltd is paused, so nothing is sent to it. Resume it first."
    )


# -- Scheduled report emails -----------------------------------------------------------------


def test_the_worker_queues_due_emails_instead_of_sending_them(app, db, cfg, vault, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    monkeypatch.setattr(delivery.export, "to_pdf", lambda html, base: b"%PDF-1.7")
    seed_tenant(db, alpha)
    _smtp_ready(db)
    contact = delivery.add_contact(db, alpha, "IT", "it@alpha.example")
    _schedule(db, alpha, [contact], include_xlsx=0)
    now = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
    db.execute("UPDATE runs SET finished_at = ?", (now.strftime("%Y-%m-%dT%H:%M:%SZ"),))
    assert delivery.tick(app, db, vault, cfg, now, queue=True) == 1
    assert sends() == []  # the main loop only queued it
    assert delivery.tick(app, db, vault, cfg, now + timedelta(minutes=1), queue=True) == 0  # already queued
    assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert tasks.drain(app, db, vault, cfg) == 1
    assert len(sends()) == 1
    assert db.execute("SELECT last_period FROM email_schedules").fetchone()[0] == "2026-10"
    assert delivery.tick(app, db, vault, cfg, now + timedelta(minutes=2), queue=True) == 0


def test_a_queued_email_is_checked_again_before_it_goes(app, db, cfg, vault, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    monkeypatch.setattr(delivery.export, "to_pdf", lambda html, base: b"%PDF-1.7")
    seed_tenant(db, alpha)
    _smtp_ready(db)
    contact = delivery.add_contact(db, alpha, "IT", "it@alpha.example")
    _schedule(db, alpha, [contact], include_xlsx=0)
    now = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
    db.execute("UPDATE runs SET finished_at = ?", (now.strftime("%Y-%m-%dT%H:%M:%SZ"),))
    delivery.tick(app, db, vault, cfg, now, queue=True)
    db.execute("UPDATE tenants SET enabled = 0")  # paused while it waited
    tasks.drain(app, db, vault, cfg)
    assert sends() == []
    assert '"skipped": true' in db.execute("SELECT result FROM tasks").fetchone()[0]


def test_worker_once_runs_the_tasks_it_queues(db, cfg, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    monkeypatch.setattr(delivery.export, "to_pdf", lambda html, base: b"%PDF-1.7")
    seed_tenant(db, alpha)
    _smtp_ready(db)
    contact = delivery.add_contact(db, alpha, "IT", "it@alpha.example")
    _schedule(db, alpha, [contact], include_xlsx=0, hour=0, day_of_month=1)
    db.execute("UPDATE runs SET finished_at = ?", (store.utcnow(),))
    worker.run_worker(cfg, once=True, graph_factory=lambda *a: None, exchange_factory=lambda *a: None)
    assert len(sends()) == 1
    assert db.execute("SELECT status FROM tasks").fetchone()[0] == "succeeded"


# -- Big PDFs --------------------------------------------------------------------------------


def test_pdfs_keep_the_first_rows_of_a_big_table(app, monkeypatch):
    big = Section("Files", ["Name"], rows=[[f"f{i}"] for i in range(25)], flags=["high", None] * 12 + [None])
    small = Section("Sites", ["Name"], rows=[["s"]], flags=[None])
    report = DataReport(
        "files", "Files", "", {"name": "Alpha", "tenant_ref": "a"}, datetime.now(UTC), sections=[big, small]
    )
    capped = printing.cap_rows(report, 10)
    assert len(capped.sections[0].rows) == 10 and capped.sections[1] is small
    assert "first 10 of 25 rows" in capped.sections[0].intro and "XLSX" in capped.sections[0].intro
    assert len(report.sections[0].rows) == 25  # the page and the spreadsheet keep every row
    assert printing.cap_rows(capped, 10) is capped

    monkeypatch.setattr(printing, "PDF_MAX_ROWS", 10)
    monkeypatch.setattr(printing.export, "pdf_available", lambda: True)
    html = []
    monkeypatch.setattr(printing.export, "to_pdf", lambda h, base: html.append(h) or b"%PDF")
    with app.test_request_context():
        from flask import g

        g.db, g.user = None, None
        printing.pdf_response("reports/detail.html", "files", report=report)
    assert "first 10 of 25 rows" in html[0] and "f9" in html[0] and "f10<" not in html[0]
    # The heading counts the whole table, as the page and the spreadsheet do.
    assert '<span class="count">25</span> <span class="pill warn">12 to review</span>' in html[0]


def test_pdfs_cap_rollups_and_change_reports(app, monkeypatch):
    from officesentry.changes import Change
    from officesentry.reports.month_changes import ReportChanges, RowChange, SectionChanges, TenantChanges
    from officesentry.reports.rollups import Column, Rollup, RollupRow

    tenants = [{"id": i, "name": f"T{i}", "tenant_ref": str(i)} for i in range(25)]
    rows = [RollupRow(t, {"n": 1}, attention=t["id"] % 2) for t in tenants]
    rollup = Rollup("mfa", "MFA", "", [Column("n", "N")], rows, datetime.now(UTC))
    capped = printing.cap_rollup(rollup, 10)
    assert len(capped.rows) == 10 and "first 10 of 25 clients" in capped.footnote and len(rollup.rows) == 25
    assert printing.cap_rollup(capped, 10) is capped

    changes = [RowChange("added", [f"row {i}"], columns=["Name"]) for i in range(25)]
    tc = TenantChanges(
        tenants[0],
        datetime.now(UTC),
        datetime.now(UTC),
        [],
        [{"severity": "high", "rule_key": "x", "subject": "s"}] * 12,
        [],
        [Change("Accounts", "added", "ann@x.com", "New account")] * 5,
        [
            ReportChanges("a", "A", [SectionChanges("One", ["Name"], changes[:15])]),
            ReportChanges("b", "B", [SectionChanges("Two", ["Name"], changes[15:])]),
        ],
        None,
    )
    cut = printing.cap_changes(tc, 10)
    assert cut.report_changes == 10 and cut.reports[0].count == 10 and cut.reports[1].count == 0
    assert len(cut.new_findings) == 10 and tc.report_changes == 25 and len(tc.new_findings) == 12
    assert [n[:45] for n in cut.notes] == [
        "This PDF shows the first 10 of 12 new finding",
        "This PDF shows the first 10 of 25 changes in ",
    ]
    assert printing.cap_changes(cut, 10) is cut

    monkeypatch.setattr(printing, "PDF_MAX_ROWS", 10)
    monkeypatch.setattr(printing.export, "pdf_available", lambda: True)
    html = []
    monkeypatch.setattr(printing.export, "to_pdf", lambda h, base: html.append(h) or b"%PDF")
    with app.test_request_context():
        from flask import g

        g.db, g.user = None, None
        printing.pdf_response("reports/rollup.html", "mfa", rollup=rollup, key="mfa")
        printing.pdf_response("reports/tenant_changes.html", "changes", report=tc, title="What changed", titles={})
        from officesentry.reports.month_changes import Summary

        summaries = [Summary(t, 1, 0, 0, 0, {}, True, ["Accounts"]) for t in tenants]
        totals = {"changed": 25, "new_severe": 0, "new": 0, "fixed": 0, "score": None, "no_baseline": 0}
        printing.pdf_response(
            "reports/changes.html",
            "changes",
            summaries=summaries,
            totals=totals,
            days=30,
            generated_at=datetime.now(UTC),
        )
    assert "first 10 of 25 clients" in html[0] and "T9<" in html[0] and "T10<" not in html[0]
    # The figures and the total count every client, as the page does.
    assert '<span class="count">25</span>' in html[0] and "<strong>25</strong>" in html[0]
    assert re.search(r"Need attention</div>\s*<div[^>]*><span>12<", html[0])
    # The change report counts every change and finding, not the ones the PDF keeps.
    assert re.search(r"Changes</div>\s*<div[^>]*><span>30<", html[1]) and "5 key, 25 in detail reports" in html[1]
    assert re.search(r"New findings</div>\s*<div[^>]*><span>12<", html[1])
    assert "first 10 of 25 clients" in html[2] and "T9<" in html[2] and "T10<" not in html[2]
    assert '<span class="count">25</span>' in html[2] and "of 25" in html[2]
