import base64
import json
import socket
from datetime import UTC, datetime, timedelta

import pytest
import responses

from officesentry import alerts, delivery, mailer, store
from officesentry import db as db_module
from officesentry.db import connect, migrate
from officesentry.reports.export import pdf_available
from tests.conftest import login, make_user
from tests.seed import seed_tenant

SENT = []


class FakeSMTP:
    fail_with = None

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

    def send_message(self, msg, from_addr=None, to_addrs=None):
        if FakeSMTP.fail_with:
            raise FakeSMTP.fail_with
        SENT.append(("send", self.host, msg, to_addrs))


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    SENT.clear()
    FakeSMTP.fail_with = None
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)


def sends():
    return [s for s in SENT if s[0] == "send"]


def _configure(client, **overrides):
    data = {
        "mail_transport": "smtp",
        "smtp_host": "smtp.example.com",
        "smtp_port": "587",
        "smtp_security": "starttls",
        "smtp_user": "reports@msp.com",
        "smtp_password": "s3cret",
        "smtp_from": "reports@msp.com",
        "msp_name": "Contoso IT",
    }
    data.update(overrides)
    return client.post("/admin/email", data=data)


def _smtp_ready(db):
    for key, value in {"smtp_host": "smtp.example.com", "smtp_from": "r@msp.com", "smtp_security": "none"}.items():
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))


def _schedule(
    db,
    tenant_id,
    contacts,
    kind="report",
    reports=("review",),
    frequency="monthly",
    day_of_week=0,
    day_of_month=1,
    hour=8,
    include_xlsx=1,
    last_period=None,
    timezone="UTC",
):
    sid = db.execute(
        "INSERT INTO email_schedules (kind, tenant_id, reports, include_xlsx, frequency, day_of_week, day_of_month,"
        " hour, timezone, last_period, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-01-01T00:00:00Z')",
        (
            kind,
            tenant_id,
            json.dumps(list(reports)),
            include_xlsx,
            frequency,
            day_of_week,
            day_of_month,
            hour,
            timezone,
            last_period,
        ),
    ).lastrowid
    delivery.set_recipients(db, "schedule", sid, contacts)
    return sid


def _collected(db, tenant_id, finished="2026-10-01T00:00:00Z"):
    """One finished collection, so the tenant has a report to send (``delivery.nothing_collected``)."""
    run_id = store.enqueue_run(db, tenant_id, "users")
    db.execute("UPDATE runs SET status = 'succeeded', finished_at = ? WHERE id = ?", (finished, run_id))


def _row(db, table, row_id):
    return db.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()


# -- Settings and transports -------------------------------------------------------------


def test_recipient_parsing():
    assert mailer.parse_recipients("a@x.com; b@y.co.uk\nA@x.com") == ["a@x.com", "b@y.co.uk", "A@x.com"]
    assert mailer.parse_recipients("not-an-email") is None
    assert mailer.parse_recipients("") is None


def test_email_settings_store_encrypted_password(client, db):
    make_user(db, "admin")
    login(client, "admin")
    _configure(client)
    stored = mailer.mail_settings(db)
    assert stored["smtp_host"] == "smtp.example.com" and stored["msp_name"] == "Contoso IT"
    assert "s3cret" not in stored["smtp_password_enc"]
    assert client.get("/admin/email").status_code == 200


def test_changing_host_clears_password(client, db):
    make_user(db, "admin")
    login(client, "admin")
    _configure(client)
    _configure(client, smtp_host="evil.example.com", smtp_password="")
    assert mailer.mail_settings(db)["smtp_password_enc"] == ""


def test_password_isnt_kept_when_security_or_port_changes(client, db):
    make_user(db, "admin")
    login(client, "admin")
    for change in ({"smtp_security": "ssl", "smtp_port": "587"}, {"smtp_port": "2525"}):
        _configure(client)
        _configure(client, smtp_password="", **change)
        assert mailer.mail_settings(db)["smtp_password_enc"] == "", change


def test_no_password_over_an_unencrypted_connection(client, db, vault):
    make_user(db, "admin")
    login(client, "admin")
    _configure(client)
    # Switching an existing server to no encryption (say, to troubleshoot on port 25) is refused...
    page = _configure(client, smtp_security="none", smtp_port="25", smtp_password="").get_data(as_text=True)
    stored = mailer.mail_settings(db)
    assert stored["smtp_security"] == "starttls" and vault.decrypt(stored["smtp_password_enc"]) == "s3cret"
    page = client.get("/admin/email").get_data(as_text=True)
    assert "sent unencrypted" in page
    # ...and even if the settings say so, the password is never sent in clear to another machine.
    db.execute("UPDATE settings SET value = 'none' WHERE key = 'smtp_security'")
    with pytest.raises(mailer.MailError, match="unencrypted"):
        mailer.send(db, vault, mailer.Email(to=["a@client.com"], subject="s", html="<p>x</p>", text="x"))
    assert not SENT
    # A relay on this same machine may take a password without TLS.
    db.execute("UPDATE settings SET value = '127.0.0.1' WHERE key = 'smtp_host'")
    mailer.send(db, vault, mailer.Email(to=["a@client.com"], subject="s", html="<p>x</p>", text="x"))
    assert SENT[0] == ("login", "reports@msp.com", "s3cret")
    assert _configure(client, smtp_host="localhost", smtp_security="none", smtp_port="25").status_code == 302
    assert mailer.mail_settings(db)["smtp_security"] == "none"


def test_graph_settings_keep_secret_only_for_the_same_app(client, db, vault):
    make_user(db, "admin")
    login(client, "admin")
    form = {
        "mail_transport": "graph",
        "graph_tenant": "msp.onmicrosoft.com",
        "graph_client_id": "11111111-2222-3333-4444-555555555555",
        "graph_sender": "reports@msp.com",
        "graph_secret": "shh",
    }
    client.post("/admin/email", data=form)
    s = mailer.mail_settings(db)
    assert mailer.transport(s) == "graph" and mailer.is_configured(s)
    assert vault.decrypt(s["graph_secret_enc"]) == "shh"
    client.post("/admin/email", data=form | {"graph_secret": ""})
    assert vault.decrypt(mailer.mail_settings(db)["graph_secret_enc"]) == "shh"
    client.post(
        "/admin/email", data=form | {"graph_secret": "", "graph_client_id": "99999999-2222-3333-4444-555555555555"}
    )
    assert mailer.mail_settings(db)["graph_secret_enc"] == ""


def test_test_email_is_logged(client, db):
    make_user(db, "admin")
    login(client, "admin")
    _configure(client)
    client.post("/admin/email", data={"action": "test", "test_to": "me@msp.com"})
    msg = sends()[0][2]
    assert msg["From"] == "Contoso IT <reports@msp.com>"
    assert msg.get_body(("html",)) is not None
    row = db.execute("SELECT * FROM email_deliveries").fetchone()
    assert row["kind"] == "test" and row["status"] == "sent" and row["transport"] == "smtp"


def _graph_db(db, vault):
    for key, value in {
        "mail_transport": "graph",
        "graph_tenant": "msp.onmicrosoft.com",
        "graph_client_id": "11111111-2222-3333-4444-555555555555",
        "graph_secret_enc": vault.encrypt("shh"),
        "graph_sender": "reports@msp.com",
    }.items():
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))


@responses.activate
def test_graph_send_mail(db, vault, monkeypatch):
    monkeypatch.setattr(mailer, "_graph_token", lambda s, v: "mail-token")
    _graph_db(db, vault)
    responses.post("https://graph.microsoft.com/v1.0/users/reports@msp.com/sendMail", status=202)
    email = mailer.Email(
        to=["it@client.com"],
        bcc=["me@msp.com"],
        subject="Hi",
        text="t",
        html="<p>h</p>",
        attachments=[("r.pdf", "application/pdf", b"%PDF-1")],
    )
    assert mailer.send(db, vault, email) == "graph"
    req = responses.calls[0].request
    assert req.headers["Authorization"] == "Bearer mail-token"
    msg = json.loads(req.body)["message"]
    assert msg["toRecipients"] == [{"emailAddress": {"address": "it@client.com"}}]
    assert msg["bccRecipients"] == [{"emailAddress": {"address": "me@msp.com"}}]
    assert msg["body"] == {"contentType": "HTML", "content": "<p>h</p>"}
    assert base64.b64decode(msg["attachments"][0]["contentBytes"]) == b"%PDF-1"


@responses.activate
def test_graph_errors_explain_what_to_fix(db, vault, monkeypatch):
    monkeypatch.setattr(mailer, "_graph_token", lambda s, v: "mail-token")
    _graph_db(db, vault)
    responses.post(
        "https://graph.microsoft.com/v1.0/users/reports@msp.com/sendMail",
        status=403,
        json={"error": {"code": "ErrorAccessDenied", "message": "Access is denied."}},
    )
    with pytest.raises(mailer.MailError, match="Mail.Send"):
        mailer.send(db, vault, mailer.Email(to=["a@x.com"], subject="s", text="t"))
    big = mailer.Email(
        to=["a@x.com"], subject="s", text="t", attachments=[("x.pdf", "application/pdf", b"0" * 3_000_000)]
    )
    with pytest.raises(mailer.MailError, match="3 MB"):
        mailer.send(db, vault, big)
    assert len(responses.calls) == 1  # too-large mail never leaves


class FakeTime:
    """A clock for ``mailer.Pacer`` that moves only when it's slept on."""

    def __init__(self):
        self.now, self.slept = 1000.0, []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def paced(monkeypatch) -> FakeTime:
    fake = FakeTime()
    monkeypatch.setattr(mailer, "PACER", mailer.Pacer(clock=fake.clock, sleep=fake.sleep))
    return fake


GRAPH_SEND = "https://graph.microsoft.com/v1.0/users/reports@msp.com/sendMail"
THROTTLED = {"error": {"code": "ApplicationThrottled", "message": "Too many messages sent in a short time."}}


def test_the_pacer_keeps_a_sender_under_microsofts_rate():
    fake = FakeTime()
    pacer = mailer.Pacer(clock=fake.clock, sleep=fake.sleep)
    for _ in range(mailer.PER_MINUTE):
        pacer.turn("Reports@msp.com", fake.now + 600)
        fake.now += 1
    pacer.turn("alerts@msp.com", fake.now + 600)  # another mailbox has its own minute
    assert fake.slept == []
    pacer.turn("reports@msp.com", fake.now + 600)  # the 31st waits until the first is a minute old
    assert fake.slept == [30] and fake.now == 1060
    pacer.turn("reports@msp.com", fake.now)  # with no patience it goes at once, and Microsoft decides
    assert fake.slept == [30]
    pacer.refused("reports@msp.com", 120)  # Microsoft said wait: every later message waits too
    pacer.turn("reports@msp.com", fake.now + 600)
    assert fake.slept == [30, 120]


@responses.activate
def test_background_sends_wait_out_microsofts_throttling(db, vault, monkeypatch):
    fake = paced(monkeypatch)
    monkeypatch.setattr(mailer, "_graph_token", lambda s, v: "mail-token")
    _graph_db(db, vault)
    responses.post(GRAPH_SEND, status=429, json=THROTTLED, headers={"Retry-After": "20"})
    responses.post(GRAPH_SEND, status=503, json={"error": {"code": "ServiceUnavailable"}})  # no Retry-After
    responses.post(GRAPH_SEND, status=202)
    with mailer.patient():
        assert mailer.send(db, vault, mailer.Email(to=["a@x.com"], subject="s", text="t")) == "graph"
    assert len(responses.calls) == 3 and fake.slept == [20, mailer.DEFAULT_RETRY_AFTER]


@responses.activate
def test_a_page_never_waits_for_microsoft(db, vault, monkeypatch):
    fake = paced(monkeypatch)
    monkeypatch.setattr(mailer, "_graph_token", lambda s, v: "mail-token")
    _graph_db(db, vault)
    responses.post(GRAPH_SEND, status=429, json=THROTTLED, headers={"Retry-After": "20"})
    responses.post(GRAPH_SEND, status=429, json=THROTTLED, headers={"Retry-After": "900"})
    responses.post(GRAPH_SEND, status=202)
    email = mailer.Email(to=["a@x.com"], subject="s", text="t")
    with pytest.raises(mailer.MailError, match="429 ApplicationThrottled.*30 messages a minute"):
        mailer.send(db, vault, email)
    assert len(responses.calls) == 1 and fake.slept == []
    # A background send after it waits out what Microsoft asked first, but not past its patience.
    with mailer.patient(), pytest.raises(mailer.MailError, match="429"):
        mailer.send(db, vault, email)
    assert len(responses.calls) == 2 and fake.slept == [20]
    with mailer.patient(1000):
        assert mailer.send(db, vault, email) == "graph"
    assert len(responses.calls) == 3 and fake.slept == [20, 900]


@responses.activate
def test_an_outage_fails_later_sends_at_once(db, vault, monkeypatch):
    """Each send waits out Microsoft for up to ten minutes; once Microsoft has refused that long, the rest try
    once and fail, so a month-end during an outage doesn't take ten minutes a client to fail."""
    fake = paced(monkeypatch)
    monkeypatch.setattr(mailer, "_graph_token", lambda s, v: "mail-token")
    _graph_db(db, vault)
    down = responses.post(GRAPH_SEND, status=503, json={"error": {"code": "ServiceUnavailable"}})
    email = mailer.Email(to=["a@x.com"], subject="s", text="t")
    with mailer.patient(), pytest.raises(mailer.MailError, match="503"):
        mailer.send(db, vault, email)
    tries = mailer.BACKGROUND_PATIENCE // mailer.DEFAULT_RETRY_AFTER + 1  # at once, then every 30 seconds
    assert down.call_count == tries and sum(fake.slept) == mailer.BACKGROUND_PATIENCE
    fake.slept.clear()
    with mailer.patient(), pytest.raises(mailer.MailError, match="503"):
        mailer.send(db, vault, email)
    assert down.call_count == tries + 1 and fake.slept == []
    # Once Microsoft takes one again, sends wait their turn as before.
    responses.replace(responses.POST, GRAPH_SEND, status=202)
    with mailer.patient():
        mailer.send(db, vault, email)
    assert mailer.PACER.refusing_for("reports@msp.com") == 0


def test_unconfigured_email_records_failure(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    cid = delivery.add_contact(db, alpha, "IT", "it@client.com")
    sid = _schedule(db, alpha, [cid])
    _collected(db, alpha)
    client.post(f"/schedules/{sid}/send")
    row = db.execute("SELECT status, error FROM email_deliveries").fetchone()
    assert row["status"] == "failed" and "isn't set up" in row["error"]
    assert _row(db, "email_schedules", sid)["last_status"] == "failed"


# -- Timing -------------------------------------------------------------------------------


def test_periods_and_due_times():
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)  # a Wednesday
    # The latest send time at or before now, and the period it belongs to.
    assert delivery.slot("weekly", 0, 1, 8, "UTC", now) == ("2026-W41", datetime(2026, 10, 5, 8, tzinfo=UTC))
    assert delivery.slot("weekly", 4, 1, 8, "UTC", now) == ("2026-W40", datetime(2026, 10, 2, 8, tzinfo=UTC))
    assert delivery.slot("monthly", 0, 15, 9, "UTC", now) == ("2026-09", datetime(2026, 9, 15, 9, tzinfo=UTC))
    assert delivery.slot("quarterly", 0, 2, 7, "UTC", now) == ("2026-Q4", datetime(2026, 10, 2, 7, tzinfo=UTC))
    assert delivery.schedule_cron("weekly", 6, 1, 8) == "0 8 * * 0"  # Sunday
    assert delivery.schedule_cron("quarterly", 0, 3, 7) == "0 7 3 1,4,7,10 *"
    # Times are in the schedule's own timezone: 08:00 in London is 07:00 UTC in summer.
    assert delivery.slot("monthly", 0, 1, 8, "Europe/London", now)[1] == datetime(2026, 10, 1, 7, tzinfo=UTC)
    # Late on 31 Dec UTC is already January in Auckland.
    nz = delivery.slot("monthly", 0, 1, 8, "Pacific/Auckland", datetime(2026, 12, 31, 20, tzinfo=UTC))
    assert nz[0] == "2027-01"


def test_next_send(db, two_tenants):
    alpha, _ = two_tenants
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    sid = _schedule(db, alpha, [], frequency="quarterly", day_of_month=3, last_period="2026-Q4")
    assert delivery.next_send(_row(db, "email_schedules", sid), "UTC", now) == datetime(2027, 1, 3, 8, tzinfo=UTC)
    sid = _schedule(db, alpha, [], frequency="weekly", day_of_week=2, hour=9, last_period="2026-W41")
    assert delivery.next_send(_row(db, "email_schedules", sid), "UTC", now) == datetime(2026, 10, 14, 9, tzinfo=UTC)
    # Saved now, so September's send time doesn't count: next is 20 October.
    sid = _schedule(
        db,
        alpha,
        [],
        frequency="monthly",
        day_of_month=20,
        last_period=delivery.initial_period("monthly", 0, 20, 8, "UTC", now),
    )
    assert delivery.next_send(_row(db, "email_schedules", sid), "UTC", now) == datetime(2026, 10, 20, 8, tzinfo=UTC)
    assert delivery.describe(_row(db, "email_schedules", sid)) == "Monthly on the 20th at 08:00"
    # A send time that was missed (the worker was down) is due now.
    db.execute("UPDATE email_schedules SET last_period = '2026-08' WHERE id = ?", (sid,))
    assert delivery.next_send(_row(db, "email_schedules", sid), "UTC", now) == datetime(2026, 9, 20, 8, tzinfo=UTC)


# -- Schedules -------------------------------------------------------------------------------


def test_create_schedule_from_form(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    team = delivery.add_contact(db, None, "Me", "me@msp.com")
    assert client.get(f"/tenants/{alpha}/schedules/new").status_code == 200
    resp = client.post(
        f"/tenants/{alpha}/schedules/new",
        data={
            "reports": ["review", "data:mailboxes", "bogus"],
            "include_xlsx": "1",
            "frequency": "weekly",
            "day_of_week": "0",
            "day_of_month": "1",
            "hour": "0",
            "timezone": "Europe/London",
            "contacts": [str(team)],
            "new_name": "Sam",
            "new_email": "sam@client.com",
            "message": "Hello",
        },
    )
    assert resp.status_code == 302
    s = db.execute("SELECT * FROM email_schedules").fetchone()
    assert json.loads(s["reports"]) == ["review", "data:mailboxes"]
    assert s["frequency"] == "weekly" and s["timezone"] == "Europe/London" and s["message"] == "Hello"
    # Monday 00:00 has already passed this week, so the first email is next week's.
    assert s["last_period"] is not None
    emails = {c["email"] for c in delivery.recipients(db, "schedule", s["id"])}
    assert emails == {"me@msp.com", "sam@client.com"}
    page = client.get(f"/tenants/{alpha}/schedules").get_data(as_text=True)
    assert "Monthly Security" in page and "Mail forwarding" in page and "Sam" in page


def test_schedule_form_requires_reports_and_people(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    resp = client.post(
        f"/tenants/{alpha}/schedules/new",
        data={"frequency": "monthly", "day_of_week": "0", "day_of_month": "1", "hour": "8"},
    )
    page = resp.get_data(as_text=True)
    assert resp.status_code == 200 and "Choose at least one report" in page and "Choose at least one person" in page
    assert db.execute("SELECT COUNT(*) FROM email_schedules").fetchone()[0] == 0
    bad = client.post(
        f"/tenants/{alpha}/schedules/new",
        data={"reports": "review", "frequency": "monthly", "day_of_month": "31", "hour": "8"},
    )
    assert bad.status_code == 400


def test_send_now_delivers_bundle_with_hidden_copy(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    _configure(client)
    client_contact = delivery.add_contact(db, alpha, "IT", "it@client.com")
    team = delivery.add_contact(db, None, "Me", "me@msp.com")
    sid = _schedule(db, alpha, [client_contact, team], reports=("review", "data:mailboxes"))
    client.post(f"/schedules/{sid}/send")
    ((_, _, msg, to_addrs),) = sends()
    assert msg["To"] == "it@client.com" and "me@msp.com" not in str(msg)  # hidden copy is not in any header
    assert set(to_addrs) == {"it@client.com", "me@msp.com"}
    assert "Alpha Ltd" in msg["Subject"]
    html = msg.get_body(("html",)).get_content()
    assert "Alpha Ltd" in html and "Open the full report" in html
    names = [p.get_filename() for p in msg.iter_attachments()]
    assert "security-review-alpha-ltd.xlsx" in names and "mailboxes-alpha-ltd.xlsx" in names
    assert ("security-review-alpha-ltd.pdf" in names) == pdf_available()
    row = db.execute("SELECT * FROM email_deliveries").fetchone()
    assert row["status"] == "sent" and row["bcc"] == "me@msp.com" and row["trigger"] == "manual"
    assert json.loads(row["attachments"]) == names


def test_tick_sends_once_per_period(app, db, two_tenants, vault, cfg):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    _smtp_ready(db)
    sid = _schedule(db, alpha, [delivery.add_contact(db, alpha, "", "a@x.com")], day_of_month=1, hour=0)
    now = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
    assert delivery.tick(app, db, vault, cfg, now) == 1
    assert delivery.tick(app, db, vault, cfg, now + timedelta(days=3)) == 0
    assert _row(db, "email_schedules", sid)["last_period"] == "2026-10"
    # November's send time: the data is weeks old, so it is collected again first.
    nov = datetime(2026, 11, 1, 0, 5, tzinfo=UTC)
    assert delivery.tick(app, db, vault, cfg, nov) == 0
    assert db.execute("SELECT COUNT(*) FROM runs WHERE status = 'queued'").fetchone()[0] > 0
    db.execute("UPDATE runs SET status = 'succeeded', finished_at = '2026-11-01T00:20:00Z' WHERE status = 'queued'")
    assert delivery.tick(app, db, vault, cfg, nov + timedelta(minutes=20)) == 1
    assert len(sends()) == 2


def test_failed_sends_retry_with_backoff_then_give_up(app, db, two_tenants, vault, cfg):
    alpha, _ = two_tenants
    _smtp_ready(db)
    sid = _schedule(db, alpha, [delivery.add_contact(db, alpha, "", "a@x.com")], hour=0)
    _collected(db, alpha)
    FakeSMTP.fail_with = OSError("connection refused")
    now = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
    assert delivery.tick(app, db, vault, cfg, now) == 1
    s = _row(db, "email_schedules", sid)
    assert s["attempts"] == 1 and s["retry_at"] == "2026-10-02T09:05:00Z" and s["last_period"] is None
    assert delivery.tick(app, db, vault, cfg, now + timedelta(minutes=1)) == 0  # waiting for the retry
    for minutes in (5, 40, 170):
        assert delivery.tick(app, db, vault, cfg, now + timedelta(minutes=minutes)) == 1
    s = _row(db, "email_schedules", sid)
    assert s["last_period"] == "2026-10" and s["last_status"] == "failed" and s["attempts"] == 0
    assert db.execute("SELECT COUNT(*) FROM email_deliveries WHERE status = 'failed'").fetchone()[0] == 4
    error = db.execute("SELECT error FROM email_deliveries").fetchone()[0]
    assert error.count("smtp.example.com") == 1 and "connection refused" in error
    # The Retry button sends it again and clears the failure.
    FakeSMTP.fail_with = None
    failed = db.execute("SELECT * FROM email_deliveries ORDER BY id DESC").fetchone()
    assert delivery.retry_delivery(app, db, vault, failed, "admin") is None
    assert _row(db, "email_schedules", sid)["last_status"] == "sent"


def test_paused_and_empty_schedules_do_not_send(app, db, two_tenants, vault, cfg):
    alpha, _ = two_tenants
    _smtp_ready(db)
    _schedule(db, alpha, [], hour=0)
    sid = _schedule(db, alpha, [delivery.add_contact(db, alpha, "", "a@x.com")], hour=0)
    db.execute("UPDATE email_schedules SET enabled = 0 WHERE id = ?", (sid,))
    assert delivery.tick(app, db, vault, cfg, datetime(2026, 10, 2, 9, tzinfo=UTC)) == 0
    assert db.execute("SELECT COUNT(*) FROM email_deliveries").fetchone()[0] == 0


def test_build_error_is_recorded_not_raised(app, db, two_tenants, vault, cfg, monkeypatch):
    alpha, _ = two_tenants
    _smtp_ready(db)
    sid = _schedule(db, alpha, [delivery.add_contact(db, alpha, "", "a@x.com")], hour=0)
    _collected(db, alpha)

    def boom(*a, **k):
        raise ValueError("bad data")

    monkeypatch.setattr(delivery.monthly_review, "build", boom)
    assert delivery.tick(app, db, vault, cfg, datetime(2026, 10, 2, 9, tzinfo=UTC)) == 1
    row = db.execute("SELECT * FROM email_deliveries").fetchone()
    assert row["status"] == "failed" and "bad data" in row["error"] and row["schedule_id"] == sid


# -- Digest ---------------------------------------------------------------------------------


def test_digest_goes_to_team_only(client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    _configure(client)
    team = delivery.add_contact(db, None, "Me", "me@msp.com")
    outsider = delivery.add_contact(db, alpha, "Client", "it@client.com")
    resp = client.post(
        "/emails/digests/new",
        data={
            "frequency": "weekly",
            "day_of_week": "0",
            "day_of_month": "1",
            "hour": "7",
            "include_xlsx": "1",
            "contacts": [str(team), str(outsider)],
        },
    )
    assert resp.status_code == 302
    sid = db.execute("SELECT id FROM email_schedules WHERE kind = 'digest'").fetchone()[0]
    assert [c["email"] for c in delivery.recipients(db, "schedule", sid)] == ["me@msp.com"]
    client.post(f"/schedules/{sid}/send")
    ((_, _, msg, to_addrs),) = sends()
    assert to_addrs == ["me@msp.com"] and msg["Subject"].startswith("Client digest")
    html = msg.get_body(("html",)).get_content()
    assert "Alpha Ltd" in html and "Bravo Inc" in html
    assert [p.get_filename() for p in msg.iter_attachments()][0].startswith("client-digest-")
    assert client.get(f"/schedules/{sid}/preview").status_code == 200


def test_analyst_cannot_see_digests(client, db, two_tenants):
    alpha, _ = two_tenants
    sid = _schedule(db, None, [], kind="digest")
    make_user(db, "analyst", role="analyst", tenants=[alpha])
    login(client, "analyst")
    assert client.get(f"/schedules/{sid}/preview").status_code == 404
    assert client.post(f"/schedules/{sid}/send").status_code == 404
    assert client.get("/emails/digests/new").status_code == 403
    assert client.post("/emails/team", data={"email": "x@y.com"}).status_code == 403
    page = client.get("/emails").get_data(as_text=True)
    assert "Client digest" not in page


# -- Alerts -----------------------------------------------------------------------------------


def _finding(db, tenant_id, key, severity, first_seen, object_id="x", reopened_at=None, accepted=False):
    return db.execute(
        "INSERT INTO findings (tenant_id, rule_key, object_id, severity, subject, detail, first_seen, last_seen,"
        " reopened_at, accepted_at) VALUES (?, ?, ?, ?, ?, 'detail', ?, ?, ?, ?)",
        (
            tenant_id,
            key,
            object_id,
            severity,
            f"subject {object_id}",
            first_seen,
            first_seen,
            reopened_at,
            first_seen if accepted else None,
        ),
    ).lastrowid


def _alerting(app, cfg, vault):
    return alerts.Alerting(cfg.base_url, vault, app)


def test_alert_email_on_new_critical_finding(app, client, db, two_tenants, vault, cfg):
    alpha, _ = two_tenants
    tenant = store.get_tenant(db, alpha)
    _smtp_ready(db)
    make_user(db, "admin")
    login(client, "admin")
    me = delivery.add_contact(db, None, "Me", "me@msp.com")
    first = _finding(db, alpha, "admin_mfa", "critical", "2026-10-01T02:00:00Z", "first-scan")
    client.post(f"/tenants/{alpha}/alert", data={"min_severity": "critical", "contacts": [str(me)]})
    alert = db.execute("SELECT * FROM email_alerts").fetchone()
    assert alert["tenant_id"] == alpha
    alerting = _alerting(app, cfg, vault)
    # Webhook alerts are off; the email still goes out on the same event.
    assert alerts.after_sync(db, alerting, tenant, {"opened": [first]}) is None
    assert sends() == []  # the tenant's first scan never emails
    ids = [
        _finding(db, alpha, "admin_mfa", "critical", "2026-10-02T02:00:00Z", "new-one"),
        _finding(db, alpha, "admin_mfa", "high", "2026-10-02T02:00:00Z", "only-high"),
        _finding(db, alpha, "admin_mfa", "critical", "2026-10-02T02:00:00Z", "accepted", accepted=True),
    ]
    alerts.after_sync(db, alerting, tenant, {"opened": ids})
    ((_, _, msg, to_addrs),) = sends()
    assert to_addrs == ["me@msp.com"] and msg["Subject"] == "1 new critical finding in Alpha Ltd"
    html = msg.get_body(("html",)).get_content()
    assert "subject new-one" in html and "subject only-high" not in html and "subject accepted" not in html
    alerts.after_sync(db, alerting, tenant, {"opened": []})
    assert len(sends()) == 1  # nothing opened, nothing sent
    # A finding that was fixed and came back emails again.
    db.execute("UPDATE findings SET reopened_at = '2026-10-03T02:00:00Z' WHERE id = ?", (first,))
    alerts.after_sync(db, alerting, tenant, {"opened": [first]})
    assert len(sends()) == 2 and "has come back" in sends()[1][2].get_body(("html",)).get_content()
    payload = json.loads(db.execute("SELECT payload FROM email_deliveries ORDER BY id DESC").fetchone()[0])
    assert payload == [first]
    assert client.get(f"/alerts/{alert['id']}/preview").status_code == 200


def test_global_alert_emails_each_tenant(app, db, two_tenants, vault, cfg):
    alpha, bravo = two_tenants
    _smtp_ready(db)
    aid = db.execute(
        "INSERT INTO email_alerts (tenant_id, min_severity, created_at) VALUES (NULL, 'high', '2026-10-01T00:00:00Z')"
    ).lastrowid
    delivery.set_recipients(db, "alert", aid, [delivery.add_contact(db, None, "", "me@msp.com")])
    alerting = _alerting(app, cfg, vault)
    for t in (alpha, bravo):
        _finding(db, t, "admin_mfa", "low", "2026-09-01T00:00:00Z", "old")
        new = _finding(db, t, "admin_mfa", "high", "2026-10-02T00:00:00Z", f"new-{t}")
        alerts.after_sync(db, alerting, store.get_tenant(db, t), {"opened": [new]})
    assert [m[2]["Subject"] for m in sends()] == [
        "1 new critical or high finding in Alpha Ltd",
        "1 new critical or high finding in Bravo Inc",
    ]


@responses.activate
def test_webhook_and_email_alerts_share_one_event(app, db, two_tenants, vault, cfg, monkeypatch):
    alpha, _ = two_tenants
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("52.1.2.3", port))],
    )
    hook = "https://hooks.example.com/run"
    responses.add(responses.POST, hook, status=202)
    alerts.save_settings(
        db,
        {
            "alerts.enabled": "1",
            "alerts.format": "slack",
            "alerts.min_severity": "high",
            "alerts.url_enc": vault.encrypt(hook),
        },
    )
    _smtp_ready(db)
    aid = db.execute(
        "INSERT INTO email_alerts (tenant_id, min_severity, created_at) VALUES (?, 'critical', '2026-10-01T00:00:00Z')",
        (alpha,),
    ).lastrowid
    delivery.set_recipients(db, "alert", aid, [delivery.add_contact(db, alpha, "", "it@alpha.com")])
    _finding(db, alpha, "admin_mfa", "low", "2026-09-01T00:00:00Z", "old")
    new = _finding(db, alpha, "admin_mfa", "critical", "2026-10-02T00:00:00Z", "new")
    run = db.execute(
        "INSERT INTO runs (tenant_id, collector, status, trigger, created_at)"
        " VALUES (?, 'users', 'succeeded', 'schedule', '2026-10-02T00:00:00Z')",
        (alpha,),
    ).lastrowid
    assert alerts.after_sync(db, _alerting(app, cfg, vault), store.get_tenant(db, alpha), {"opened": [new]}, run)
    assert len(responses.calls) == 1 and len(sends()) == 1
    events = [r[0] for r in db.execute("SELECT message FROM run_events WHERE run_id = ?", (run,))]
    assert "Alert sent" in events and "Alert email sent" in events


def test_failed_alert_email_retries_then_gives_up(app, db, two_tenants, vault, cfg):
    alpha, _ = two_tenants
    tenant = store.get_tenant(db, alpha)
    _smtp_ready(db)
    aid = db.execute(
        "INSERT INTO email_alerts (tenant_id, min_severity, created_at) VALUES (?, 'critical', '2026-10-01T00:00:00Z')",
        (alpha,),
    ).lastrowid
    delivery.set_recipients(db, "alert", aid, [delivery.add_contact(db, alpha, "", "it@alpha.com")])
    _finding(db, alpha, "admin_mfa", "low", "2026-09-01T00:00:00Z", "old")
    new = _finding(db, alpha, "admin_mfa", "critical", "2026-10-02T00:00:00Z", "new")
    FakeSMTP.fail_with = OSError("connection refused")
    alerts.after_sync(db, _alerting(app, cfg, vault), tenant, {"opened": [new]})
    first = db.execute("SELECT * FROM email_deliveries").fetchone()
    assert first["status"] == "failed" and first["attempt"] == 1 and first["retry_at"]
    now = datetime.fromisoformat(first["retry_at"].replace("Z", "+00:00"))
    assert delivery.tick(app, db, vault, cfg, now - timedelta(minutes=1)) == 0  # waiting for the retry
    for minutes in (0, 31, 152):
        assert delivery.tick(app, db, vault, cfg, now + timedelta(minutes=minutes)) == 1
    rows = db.execute("SELECT attempt, retry_at FROM email_deliveries ORDER BY id").fetchall()
    assert [r["attempt"] for r in rows] == [1, 2, 3, 4]
    assert [r["retry_at"] is None for r in rows] == [True, True, True, True]  # given up after the 4th
    assert delivery.tick(app, db, vault, cfg, now + timedelta(days=1)) == 0
    # The Retry button sends what is still open; once it's fixed there's nothing to send.
    FakeSMTP.fail_with = None
    last = db.execute("SELECT * FROM email_deliveries ORDER BY id DESC").fetchone()
    assert delivery.retry_delivery(app, db, vault, last, "admin") is None and len(sends()) == 1
    db.execute("UPDATE findings SET resolved_at = '2026-10-03T00:00:00Z' WHERE id = ?", (new,))
    assert "fixed or accepted" in delivery.retry_delivery(app, db, vault, last, "admin")


# -- Pages and access ----------------------------------------------------------------------------


def test_preview_renders_email_in_a_locked_down_frame(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    sid = _schedule(db, alpha, [delivery.add_contact(db, alpha, "IT", "it@client.com")], reports=("review", "evidence"))
    page = client.get(f"/schedules/{sid}/preview")
    assert page.status_code == 200 and page.headers["X-Frame-Options"] == "DENY"
    text = page.get_data(as_text=True)
    assert "it@client.com" in text and "security-evidence-alpha-ltd" in text
    body = client.get(f"/schedules/{sid}/preview/body")
    assert body.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert "script-src" not in body.headers["Content-Security-Policy"]
    assert "frame-ancestors 'self'" in body.headers["Content-Security-Policy"]
    assert "Alpha Ltd" in body.get_data(as_text=True)
    assert db.execute("SELECT COUNT(*) FROM email_deliveries").fetchone()[0] == 0  # previews never send


def test_overview_page(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    _schedule(db, alpha, [delivery.add_contact(db, alpha, "IT", "it@client.com")])
    page = client.get("/emails").get_data(as_text=True)
    assert "Alpha Ltd" in page and "Set up a digest" in page and "Your team" in page


def test_client_sees_their_emails_but_not_internal_details(client, db, two_tenants):
    alpha, bravo = two_tenants
    sid = _schedule(
        db,
        alpha,
        [
            delivery.add_contact(db, alpha, "Client IT", "it@client.com"),
            delivery.add_contact(db, None, "Secret Tech", "tech@msp.com"),
        ],
    )
    db.execute(
        "INSERT INTO email_deliveries (kind, schedule_id, tenant_id, subject, recipients, bcc, status, error,"
        " created_at) VALUES ('report', ?, ?, 'S', 'it@client.com', 'tech@msp.com', 'failed',"
        " 'smtp.internal refused', '2026-10-01T00:00:00Z')",
        (sid, alpha),
    )
    # The team in the To line (no client contact then), and an alert, which only ever goes to the team.
    for kind, to in (
        ("report", "it@client.com, lead@msp.com"),
        ("report", "night@msp.com"),
        ("alert", "oncall@msp.com"),
    ):
        db.execute(
            "INSERT INTO email_deliveries (kind, schedule_id, tenant_id, subject, recipients, status,"
            " created_at) VALUES (?, ?, ?, 'S', ?, 'sent', '2026-10-02T00:00:00Z')",
            (kind, sid, alpha, to),
        )
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    page = client.get(f"/tenants/{alpha}/schedules").get_data(as_text=True)
    assert "Client IT" in page
    assert "tech@msp.com" not in page and "Secret Tech" not in page and "smtp.internal" not in page
    assert "@msp.com" not in page and page.count('class="wrap small">it@client.com<') == 2
    assert client.get(f"/tenants/{bravo}/schedules").status_code == 404
    assert client.get(f"/tenants/{alpha}/schedules/new").status_code == 403
    assert client.post(f"/schedules/{sid}/send").status_code == 403
    assert client.get(f"/schedules/{sid}/preview").status_code == 403
    assert client.get("/emails").status_code == 403
    assert client.get("/admin/email").status_code == 403


def test_analyst_is_kept_to_granted_tenants(client, db, two_tenants):
    alpha, bravo = two_tenants
    bravo_contact = delivery.add_contact(db, bravo, "", "b@bravo.com")
    bravo_schedule = _schedule(db, bravo, [bravo_contact])
    make_user(db, "analyst", role="analyst", tenants=[alpha])
    login(client, "analyst")
    for url in (f"/schedules/{bravo_schedule}/edit", f"/schedules/{bravo_schedule}/preview"):
        assert client.get(url).status_code == 404
    assert client.post(f"/contacts/{bravo_contact}/delete").status_code == 404
    # Another tenant's contact can't be slipped into this tenant's email.
    own = delivery.add_contact(db, alpha, "", "a@alpha.com")
    client.post(
        f"/tenants/{alpha}/schedules/new",
        data={
            "reports": "review",
            "frequency": "monthly",
            "day_of_week": "0",
            "day_of_month": "1",
            "hour": "8",
            "contacts": [str(own), str(bravo_contact)],
        },
    )
    sid = db.execute("SELECT id FROM email_schedules WHERE tenant_id = ?", (alpha,)).fetchone()[0]
    assert [c["email"] for c in delivery.recipients(db, "schedule", sid)] == ["a@alpha.com"]
    assert "b@bravo.com" not in client.get("/emails").get_data(as_text=True)


def test_pause_resume_edit_and_delete(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    sid = _schedule(db, alpha, [delivery.add_contact(db, alpha, "", "a@x.com")])
    client.post(f"/schedules/{sid}/toggle")
    assert _row(db, "email_schedules", sid)["enabled"] == 0
    assert "Paused" in client.get(f"/tenants/{alpha}/schedules").get_data(as_text=True)
    # Editing a paused schedule keeps it paused.
    client.post(
        f"/schedules/{sid}/edit",
        data={
            "reports": ["review"],
            "frequency": "monthly",
            "day_of_week": "0",
            "day_of_month": "2",
            "hour": "7",
            "contacts": [str(c["id"]) for c in delivery.tenant_contacts(db, alpha)],
        },
    )
    assert (_row(db, "email_schedules", sid)["enabled"], _row(db, "email_schedules", sid)["day_of_month"]) == (0, 2)
    client.post(f"/schedules/{sid}/toggle")
    assert _row(db, "email_schedules", sid)["enabled"] == 1
    assert client.get(f"/schedules/{sid}/edit").status_code == 200
    client.post(
        f"/schedules/{sid}/edit",
        data={
            "reports": ["evidence"],
            "frequency": "quarterly",
            "day_of_week": "0",
            "day_of_month": "5",
            "hour": "6",
            "contacts": [str(c["id"]) for c in delivery.tenant_contacts(db, alpha)],
        },
    )
    s = _row(db, "email_schedules", sid)
    assert s["frequency"] == "quarterly" and json.loads(s["reports"]) == ["evidence"] and s["enabled"] == 1
    client.post(f"/schedules/{sid}/delete")
    assert _row(db, "email_schedules", sid) is None


# -- Upgrading from the first schema ----------------------------------------------------------------


def test_migration_carries_over_monthly_schedules(cfg, monkeypatch):
    path = cfg.data_dir / "upgrade.db"
    conn = connect(path)
    every = db_module.migration_files()
    monkeypatch.setattr(db_module, "migration_files", lambda: [m for m in every if m[0] <= 4])
    migrate(conn)
    conn.execute("INSERT INTO tenants (name, tenant_ref, created_at) VALUES ('A', 'a.onmicrosoft.com', 'x')")
    conn.execute(
        "INSERT INTO report_schedules (tenant_id, recipients, day_of_month, hour, last_period, created_at)"
        " VALUES (1, 'a@x.com, b@y.com', 3, 9, '2026-09', '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO report_deliveries (schedule_id, tenant_id, recipients, sent_at, status)"
        " VALUES (1, 1, 'a@x.com, b@y.com', '2026-09-03T09:00:00Z', 'sent')"
    )
    monkeypatch.setattr(db_module, "migration_files", lambda: every)
    migrate(conn)
    s = conn.execute("SELECT * FROM email_schedules").fetchone()
    assert (s["frequency"], s["day_of_month"], s["hour"], s["last_period"]) == ("monthly", 3, 9, "2026-09")
    assert sorted(c["email"] for c in delivery.recipients(conn, "schedule", s["id"])) == ["a@x.com", "b@y.com"]
    assert conn.execute("SELECT status FROM email_deliveries").fetchone()[0] == "sent"
    conn.close()


# -- Month boundaries, retries and duplicate sends ---------------------------------------------


def _capture_pdfs(monkeypatch) -> list[str]:
    html = []
    monkeypatch.setattr(delivery.export, "pdf_available", lambda: True)  # Windows CI has no Pango
    monkeypatch.setattr(delivery.export, "to_pdf", lambda page, base: html.append(page) or b"%PDF-1.7")
    return html


def test_a_report_sent_again_later_keeps_its_own_period_and_commentary(app, db, two_tenants, vault, cfg, monkeypatch):
    from officesentry import month_end

    alpha, _ = two_tenants
    pdfs = _capture_pdfs(monkeypatch)
    _smtp_ready(db)
    _collected(db, alpha)
    sid = _schedule(db, alpha, [delivery.add_contact(db, alpha, "", "a@x.com")], day_of_month=28, hour=9)
    month_end.update_review(db, alpha, "2026-10", "admin", commentary="October went well.")
    month_end.update_review(db, alpha, "2026-11", "admin", commentary="November notes, not ready yet.")
    # October's email failed on the 28th and is sent again on 3 November, before November's send time.
    s = _row(db, "email_schedules", sid)
    nov3 = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
    assert delivery.send_schedule(app, db, vault, s, trigger="retry", user="admin", now=nov3) is None
    ((_, _, msg, _),) = sends()
    assert msg["Subject"] == "Alpha Ltd security report · October 2026"
    assert "security report for October 2026" in msg.get_body(("plain",)).get_content()
    assert any("October went well." in page for page in pdfs)
    assert not any("November notes" in page for page in pdfs)
    row = db.execute("SELECT period FROM email_deliveries").fetchone()
    assert row["period"] == "2026-10"


def test_a_weekly_retry_after_midnight_names_the_week_it_was_for(app, db, two_tenants, vault, cfg):
    alpha, _ = two_tenants
    _smtp_ready(db)
    _collected(db, alpha)
    db.execute("UPDATE tenants SET connection_id = NULL")  # nothing to refresh first
    contact = delivery.add_contact(db, alpha, "", "a@x.com")
    sid = _schedule(
        db, alpha, [contact], reports=(), frequency="weekly", day_of_week=6, hour=23, timezone="Europe/London"
    )
    # Sunday 11 October, 23:00 in London. The first three tries fail; the third retry runs at 01:35 on Monday.
    FakeSMTP.fail_with = OSError("connection refused")
    for at in ("2026-10-11T22:00", "2026-10-11T22:05", "2026-10-11T22:35"):
        assert delivery.tick(app, db, vault, cfg, datetime.fromisoformat(at + "+00:00")) == 1
    assert _row(db, "email_schedules", sid)["retry_at"] == "2026-10-12T00:35:00Z"
    FakeSMTP.fail_with = None
    assert delivery.tick(app, db, vault, cfg, datetime(2026, 10, 12, 0, 35, tzinfo=UTC)) == 1
    ((_, _, msg, _),) = sends()
    assert msg["Subject"].endswith("week of 5 Oct 2026")  # Monday 12 October's own email would say "12 Oct"
    assert _row(db, "email_schedules", sid)["last_period"] == "2026-W41"


def test_period_labels():
    assert delivery.period_label("2026-10") == "October 2026"
    assert delivery.period_label("2026-W01") == "week of 29 Dec 2025"
    assert delivery.period_label("2026-W53") == "week of 28 Dec 2026"
    assert delivery.period_label("2026-Q4") == "Q4 2026"


def test_clocks_going_back_or_forward_send_once(app, db, two_tenants, vault, cfg):
    alpha, bravo = two_tenants
    _smtp_ready(db)
    db.execute("UPDATE tenants SET connection_id = NULL")
    for tenant_id in (alpha, bravo):
        _collected(db, tenant_id)
    # 01:00 on 25 October 2026 happens twice in London; 02:00 on 8 March 2026 never happens in New York.
    london = _schedule(
        db, alpha, [delivery.add_contact(db, alpha, "", "a@x.com")], day_of_month=25, hour=1, timezone="Europe/London"
    )
    new_york = _schedule(
        db,
        bravo,
        [delivery.add_contact(db, bravo, "", "b@x.com")],
        day_of_month=8,
        hour=2,
        timezone="America/New_York",
    )
    db.execute("UPDATE email_schedules SET last_period = '2026-02'")
    sent = [
        delivery.tick(app, db, vault, cfg, datetime(2026, 3, 8, 5, 0, tzinfo=UTC) + timedelta(minutes=m))
        for m in range(0, 240, 15)
    ]
    assert sum(sent) == 1 and _row(db, "email_schedules", new_york)["last_period"] == "2026-03"
    db.execute("UPDATE email_schedules SET last_period = '2026-10' WHERE id = ?", (new_york,))
    db.execute("UPDATE email_schedules SET last_period = '2026-09' WHERE id = ?", (london,))
    sent = [
        delivery.tick(app, db, vault, cfg, datetime(2026, 10, 25, 0, 0, tzinfo=UTC) + timedelta(minutes=m))
        for m in range(0, 180, 15)
    ]
    assert sum(sent) == 1 and _row(db, "email_schedules", london)["last_period"] == "2026-10"
    assert len(sends()) == 2


def test_a_tenant_with_no_data_is_not_sent_an_empty_report(app, db, two_tenants, vault, cfg):
    alpha, _ = two_tenants
    _smtp_ready(db)
    db.execute("UPDATE tenants SET connection_id = NULL")
    sid = _schedule(db, alpha, [delivery.add_contact(db, alpha, "", "a@x.com")], hour=0)
    assert delivery.tick(app, db, vault, cfg, datetime(2026, 10, 2, 9, tzinfo=UTC)) == 1
    assert sends() == []
    row = db.execute("SELECT * FROM email_deliveries").fetchone()
    assert row["status"] == "failed" and row["error"].startswith("Nothing has been collected for Alpha Ltd yet")
    assert _row(db, "email_schedules", sid)["retry_at"]  # tried again, in case a collection lands
    _collected(db, alpha)
    assert delivery.tick(app, db, vault, cfg, datetime(2026, 10, 2, 9, 5, tzinfo=UTC)) == 1
    assert len(sends()) == 1


def test_plain_text_leads_with_the_verdict_and_says_when_data_is_old(app, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    sid = _schedule(db, alpha, [], reports=())
    s, t = _row(db, "email_schedules", sid), store.get_tenant(db, alpha)
    fresh = delivery.build_report_email(app, db, s, t, [], attach=False).email
    verdict = delivery.summary.monthly(delivery.monthly_review.build(db, t)).verdict
    assert f"{verdict.label}: {verdict.headline}" in fresh.text
    assert fresh.text.index(verdict.headline) < fresh.text.index("Needs attention")
    assert "more than 36 hours old" not in fresh.text
    db.execute("UPDATE runs SET finished_at = '2026-01-01T00:00:00Z'")
    old = delivery.build_report_email(app, db, s, t, [], attach=False).email
    assert "more than 36 hours old" in old.text and "more than 36 hours old" in old.html


def test_digest_count_leaves_out_accepted_risks_like_its_list(db, two_tenants):
    alpha, _ = two_tenants
    now = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
    new = "2026-10-08T02:00:00Z"
    _finding(db, alpha, "admin_mfa", "critical", new, "open")
    accepted = _finding(db, alpha, "admin_mfa", "high", new, "accepted", accepted=True)
    lapsed = _finding(db, alpha, "admin_mfa", "high", new, "lapsed", accepted=True)
    db.execute("UPDATE findings SET accepted_until = '2026-10-09T00:00:00Z' WHERE id = ?", (lapsed,))
    d = delivery.digest_data(db, "weekly", now)
    assert d["new_severe"] == len(d["new_findings"]) == 2
    assert accepted not in {f["id"] for f in d["new_findings"]}


def test_pausing_and_resuming_is_one_change(client, db, two_tenants, monkeypatch):
    from officesentry.web import email as email_pages

    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    sid = _schedule(db, alpha, [delivery.add_contact(db, alpha, "", "a@x.com")], last_period="2026-01")
    client.post(f"/schedules/{sid}/toggle")
    assert _row(db, "email_schedules", sid)["enabled"] == 0

    def crash(*a, **k):
        raise RuntimeError("stopped half way")

    monkeypatch.setattr(email_pages, "audit", crash)
    with pytest.raises(RuntimeError):
        client.post(f"/schedules/{sid}/toggle")
    s = _row(db, "email_schedules", sid)
    assert (s["enabled"], s["last_period"]) == (0, "2026-01")  # nothing half done
    monkeypatch.undo()
    client.post(f"/schedules/{sid}/toggle")
    s = _row(db, "email_schedules", sid)
    assert s["enabled"] == 1 and s["last_period"] > "2026-01"  # the paused months count as done


def test_send_now_logs_a_failure_as_a_failure(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    _collected(db, alpha)
    sid = _schedule(db, alpha, [delivery.add_contact(db, alpha, "", "a@x.com")])
    client.post(f"/schedules/{sid}/send")  # email isn't set up
    actions = [r[0] for r in db.execute("SELECT action FROM audit_log WHERE action LIKE 'email_%'")]
    assert actions == ["email_send_failed"]
    _smtp_ready(db)
    client.post(f"/schedules/{sid}/send")
    actions = [r[0] for r in db.execute("SELECT action FROM audit_log WHERE action LIKE 'email_%' ORDER BY id")]
    assert actions == ["email_send_failed", "email_sent"] and len(sends()) == 1


def test_send_now_and_retry_wait_while_the_timer_is_sending(app, client, db, two_tenants, vault, cfg):
    from officesentry import tasks

    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    _smtp_ready(db)
    _collected(db, alpha)
    db.execute("UPDATE tenants SET connection_id = NULL")
    sid = _schedule(db, alpha, [delivery.add_contact(db, alpha, "", "a@x.com")])
    s = _row(db, "email_schedules", sid)
    period, _ = delivery.schedule_slot(s, cfg.timezone, datetime.now(UTC))
    tasks.enqueue(db, "send_schedule", key=delivery.send_key(s, period), tenant_id=alpha, payload={"schedule_id": sid})
    page = client.post(f"/schedules/{sid}/send", follow_redirects=True).get_data(as_text=True)
    assert "being sent on its schedule right now" in page and sends() == []
    failed = delivery.record(
        db,
        kind="report",
        email=None,
        subject="x",
        error="boom",
        transport=None,
        trigger="schedule",
        user=None,
        schedule_id=sid,
        tenant_id=alpha,
        period=period,
    )
    assert delivery.retry_delivery(app, db, vault, _row(db, "email_deliveries", failed), "admin") == delivery.BEING_SENT
    assert sends() == []
    db.execute("UPDATE tasks SET status = 'succeeded'")
    client.post(f"/schedules/{sid}/send")
    assert len(sends()) == 1
