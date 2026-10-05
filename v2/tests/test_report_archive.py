"""The report archive: every file a report email attached is kept as it was sent."""

import hashlib
from datetime import UTC, datetime

from officesentry import delivery, issued, lifecycle, store
from tests.conftest import login, make_user
from tests.seed import seed_tenant
from tests.test_email import _configure, _schedule, _smtp_ready, fake_smtp, sends  # noqa: F401 (autouse fixture)


def _attached(msg) -> dict[str, bytes]:
    return {p.get_filename(): p.get_payload(decode=True) for p in msg.iter_attachments()}


def test_sending_a_schedule_keeps_each_attachment(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    _configure(client)
    sid = _schedule(db, alpha, [delivery.add_contact(db, alpha, "IT", "it@client.com"),
                                delivery.add_contact(db, None, "Me", "me@msp.com")],
                    reports=("review", "data:mailboxes"))
    client.post(f"/schedules/{sid}/send")
    (_, _, msg, _), = sends()
    attached = _attached(msg)
    rows = db.execute("SELECT * FROM issued_reports ORDER BY id").fetchall()
    assert {r["filename"]: bytes(r["data"]) for r in rows} == attached
    for r in rows:
        assert r["tenant_id"] == alpha and r["schedule_id"] == sid and r["trigger"] == "manual"
        assert r["issued_by"] == "admin" and r["recipients"] == "it@client.com" and r["bcc"] == "me@msp.com"
        assert r["sha256"] == hashlib.sha256(bytes(r["data"])).hexdigest() and r["size"] == len(r["data"])
        assert r["period"] and r["version"] == 1
    kinds = {r["filename"]: (r["kind"], r["title"]) for r in rows}
    assert kinds["mailboxes-alpha-ltd.xlsx"][0] == "data:mailboxes"
    assert kinds["security-review-alpha-ltd.xlsx"] == ("review", delivery.catalog_map()["review"].title)
    # Sent again for the same period: a second version, the first is kept.
    client.post(f"/schedules/{sid}/send")
    versions = [r["version"] for r in db.execute(
        "SELECT version FROM issued_reports WHERE filename = 'mailboxes-alpha-ltd.xlsx' ORDER BY id")]
    assert versions == [1, 2]


def test_scheduled_send_is_kept_and_failed_send_is_not(app, db, two_tenants, vault, cfg):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    sid = _schedule(db, alpha, [delivery.add_contact(db, alpha, "", "a@x.com")], day_of_month=1, hour=0)
    now = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
    # Mail isn't set up: nothing went out, so nothing is kept.
    schedule = db.execute("SELECT * FROM email_schedules WHERE id = ?", (sid,)).fetchone()
    assert delivery.send_schedule(app, db, vault, schedule, trigger="schedule", now=now)
    assert db.execute("SELECT COUNT(*) FROM issued_reports").fetchone()[0] == 0
    _smtp_ready(db)
    db.execute("UPDATE email_schedules SET retry_at = NULL, attempts = 0 WHERE id = ?", (sid,))
    assert delivery.tick(app, db, vault, cfg, now) == 1
    rows = db.execute("SELECT * FROM issued_reports").fetchall()
    assert rows and all(r["trigger"] == "schedule" and r["period"] == "2026-10" and r["issued_by"] is None
                        for r in rows)


def _issue(db, tenant_id, filename="security-review-alpha-ltd.pdf", data=b"%PDF-1.7 report"):
    content_type = "application/pdf" if filename.endswith(".pdf") else delivery.XLSX
    return issued.archive(db, tenant_id, [issued.IssuedFile("review", "Monthly review", filename, content_type, data)],
                          period="2026-10", trigger="schedule", user=None, to=["it@client.com"],
                          bcc=["tech@msp.com"], now="2026-10-01T08:00:00Z")[0]


def test_history_page_and_download_are_tenant_scoped(client, db, two_tenants):
    alpha, bravo = two_tenants
    pdf = _issue(db, alpha)
    xlsx = _issue(db, alpha, "security-review-alpha-ltd.xlsx", b"PK\x03\x04sheet")
    other = _issue(db, bravo, "security-review-bravo-inc.pdf", b"%PDF bravo")

    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/report-history").get_data(as_text=True)
    assert "October 2026" in page and "tech@msp.com" in page and "it@client.com" in page
    assert page.index(f"/report-history/{pdf}") < page.index(f"/report-history/{xlsx}")  # PDF first
    resp = client.get(f"/tenants/{alpha}/report-history/{pdf}")
    assert resp.status_code == 200 and resp.data == b"%PDF-1.7 report" and resp.mimetype == "application/pdf"
    assert "security-review-alpha-ltd.pdf" in resp.headers["Content-Disposition"]
    assert client.get(f"/tenants/{alpha}/report-history/{xlsx}").mimetype == delivery.XLSX
    # A report id from another tenant isn't found under this one.
    assert client.get(f"/tenants/{alpha}/report-history/{other}").status_code == 404
    assert f"/tenants/{alpha}/report-history" in client.get(f"/tenants/{alpha}/schedules").get_data(as_text=True)
    client.post("/logout")

    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    page = client.get(f"/tenants/{alpha}/report-history").get_data(as_text=True)
    assert "Monthly review" in page and "tech@msp.com" not in page and "it@client.com" not in page
    assert client.get(f"/tenants/{alpha}/report-history/{pdf}").data == b"%PDF-1.7 report"
    assert client.get(f"/tenants/{bravo}/report-history").status_code == 404
    assert client.get(f"/tenants/{bravo}/report-history/{other}").status_code == 404
    assert f"/tenants/{alpha}/report-history" in client.get("/").get_data(as_text=True)


def test_empty_history(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    assert "No reports have been emailed" in client.get(f"/tenants/{alpha}/report-history").get_data(as_text=True)


def test_period_labels():
    assert issued.period_label("2026-10") == "October 2026"
    assert issued.period_label("2026-W41") == "Week 41, 2026"
    assert issued.period_label("2026-Q4") == "Q4 2026"
    assert issued.period_label(None) == ""


def test_deleting_the_tenant_deletes_its_reports(db, two_tenants):
    alpha, bravo = two_tenants
    _issue(db, alpha)
    _issue(db, bravo, "security-review-bravo-inc.pdf")
    lifecycle.delete_tenant(db, store.get_tenant(db, alpha), "admin")
    assert [r["tenant_id"] for r in db.execute("SELECT tenant_id FROM issued_reports")] == [bravo]
