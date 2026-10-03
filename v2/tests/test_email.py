from datetime import UTC, datetime

import pytest

from officesentry import mailer
from tests.conftest import login, make_user
from tests.seed import seed_tenant

SENT = []


class FakeSMTP:
    def __init__(self, host, port, timeout=None, context=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        SENT.append(("login", user, password))

    def send_message(self, msg):
        SENT.append(("send", self.host, msg))


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    SENT.clear()
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)


def _configure(client, **overrides):
    data = {"smtp_host": "smtp.example.com", "smtp_port": "587", "smtp_security": "starttls",
            "smtp_user": "reports@msp.com", "smtp_password": "s3cret", "smtp_from": "reports@msp.com"}
    data.update(overrides)
    return client.post("/admin/email", data=data)


def test_recipient_parsing():
    assert mailer.parse_recipients("a@x.com; b@y.co.uk\nA@x.com") == ["a@x.com", "b@y.co.uk", "A@x.com"]
    assert mailer.parse_recipients("not-an-email") is None
    assert mailer.parse_recipients("") is None


def test_email_settings_store_encrypted_password(client, db):
    make_user(db, "admin")
    login(client, "admin")
    _configure(client)
    stored = dict(db.execute("SELECT key, value FROM settings").fetchall())
    assert stored["smtp_host"] == "smtp.example.com"
    assert "s3cret" not in stored["smtp_password_enc"]


def test_changing_host_clears_password(client, db):
    make_user(db, "admin")
    login(client, "admin")
    _configure(client)
    _configure(client, smtp_host="evil.example.com", smtp_password="")
    assert mailer.smtp_settings(db)["smtp_password_enc"] == ""


def test_send_now_delivers_pdf_and_xlsx(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    _configure(client)
    client.post(f"/tenants/{alpha}/schedules", data={"recipients": "it@client.com", "day_of_month": "1", "hour": "8"})
    sid = db.execute("SELECT id FROM report_schedules").fetchone()[0]
    client.post(f"/schedules/{sid}/send")
    sends = [s for s in SENT if s[0] == "send"]
    assert len(sends) == 1
    msg = sends[0][2]
    assert msg["To"] == "it@client.com" and "Alpha Ltd" in msg["Subject"]
    names = [p.get_filename() for p in msg.iter_attachments()]
    assert any(n.endswith(".xlsx") for n in names)
    assert ("login", "reports@msp.com", "s3cret") in SENT
    assert db.execute("SELECT status FROM report_deliveries").fetchone()[0] == "sent"


def test_unconfigured_email_records_failure(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    client.post(f"/tenants/{alpha}/schedules", data={"recipients": "it@client.com", "day_of_month": "1", "hour": "8"})
    sid = db.execute("SELECT id FROM report_schedules").fetchone()[0]
    client.post(f"/schedules/{sid}/send")
    row = db.execute("SELECT status, error FROM report_deliveries").fetchone()
    assert row["status"] == "failed" and "isn't set up" in row["error"]


def test_due_schedules(db, two_tenants):
    alpha, _ = two_tenants
    db.execute("INSERT INTO report_schedules (tenant_id, recipients, day_of_month, hour, created_at)"
               " VALUES (?, 'a@x.com', 5, 8, '2026-10-01T00:00:00Z')", (alpha,))
    # September's send time is before the schedule existed.
    assert mailer.due_schedules(db, "UTC", datetime(2026, 10, 5, 7, 0, tzinfo=UTC)) == []
    due = mailer.due_schedules(db, "UTC", datetime(2026, 10, 5, 8, 0, tzinfo=UTC))
    assert [d[1] for d in due] == ["2026-10"]
    db.execute("UPDATE report_schedules SET last_period = '2026-10'")
    assert mailer.due_schedules(db, "UTC", datetime(2026, 10, 20, 8, 0, tzinfo=UTC)) == []
    assert len(mailer.due_schedules(db, "UTC", datetime(2026, 11, 6, 0, 0, tzinfo=UTC))) == 1


def test_run_due_sends_once_per_month(app, db, two_tenants, vault):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    for key, value in {"smtp_host": "smtp.example.com", "smtp_from": "r@msp.com", "smtp_security": "none"}.items():
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
    db.execute("INSERT INTO report_schedules (tenant_id, recipients, day_of_month, hour, created_at)"
               " VALUES (?, 'a@x.com', 1, 0, '2026-01-01T00:00:00Z')", (alpha,))
    now = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
    assert mailer.run_due(app, db, vault, "UTC", now) == 1
    assert mailer.run_due(app, db, vault, "UTC", now) == 0


def test_client_cannot_manage_schedules(client, db, two_tenants):
    alpha, bravo = two_tenants
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    assert client.get(f"/tenants/{alpha}/schedules").status_code == 200
    assert client.post(f"/tenants/{alpha}/schedules", data={"recipients": "a@x.com", "day_of_month": "1",
                                                            "hour": "1"}).status_code == 403
    assert client.get(f"/tenants/{bravo}/schedules").status_code == 404
    assert client.get("/admin/email").status_code == 403
