"""Today's ledger by email (delivery.build_ledger_email): the team's morning email, every weekday."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from officesentry import delivery, tenant_groups
from officesentry.db import utcnow
from tests.conftest import login, make_user
from tests.test_email import _configure, fake_smtp, sends  # noqa: F401  (fake_smtp: autouse, nothing is sent)

ISO = "%Y-%m-%dT%H:%M:%SZ"


def _finding(db, tenant_id, rule_key, severity, seen: datetime) -> None:
    at = seen.strftime(ISO)
    db.execute(
        "INSERT INTO findings (tenant_id, rule_key, object_id, severity, subject, detail, first_seen, last_seen)"
        " VALUES (?, ?, ?, ?, 'someone', '', ?, ?)",
        (tenant_id, rule_key, f"obj-{rule_key}", severity, at, at),
    )


def _failed_run(db, tenant_id, at: datetime) -> None:
    db.execute(
        "INSERT INTO runs (tenant_id, collector, status, trigger, created_at, finished_at, error)"
        " VALUES (?, 'users', 'failed', 'schedule', ?, ?, 'Graph 500')",
        (tenant_id, at.strftime(ISO), at.strftime(ISO)),
    )


def _schedule(**over) -> dict:
    return {
        "kind": "digest",
        "reports": '["today"]',
        "frequency": "weekly",
        "day_of_week": 0,
        "day_of_month": 1,
        "hour": 7,
        "timezone": "Europe/London",
        "enabled": 1,
        "last_period": None,
        "retry_at": None,
        **over,
    }


def test_every_weekday_whatever_frequency_it_stores():
    s = _schedule()
    assert delivery.is_ledger(s) and delivery.cadence(s) == "weekdays"
    assert delivery.describe(s) == "Every weekday at 07:00"
    friday = datetime(2026, 10, 23, 6, 30, tzinfo=UTC)  # 07:30 in London (summer time)
    assert delivery.schedule_slot(s, "UTC", friday) == ("2026-10-23", datetime(2026, 10, 23, 6, tzinfo=UTC))
    saturday = datetime(2026, 10, 24, 12, tzinfo=UTC)
    assert delivery.schedule_slot(s, "UTC", saturday)[0] == "2026-10-23"  # nothing at the weekend
    monday = delivery.next_send({**s, "last_period": "2026-10-23"}, "UTC", saturday)
    assert monday == datetime(2026, 10, 26, 7, tzinfo=UTC)  # 07:00 in London, back on GMT
    assert delivery.period_label("2026-10-26") == "Monday 26 October 2026"
    assert delivery.period_done("2026-10-26", "2026-10-23") and not delivery.period_done("2026-10-23", "2026-10-26")
    # Monday's covers the weekend, from Friday's send; a week back at most.
    assert delivery.ledger_since(s, "UTC", monday) == datetime(2026, 10, 23, 6, tzinfo=UTC)
    # Other schedules are as they were.
    weekly = _schedule(reports='["upcoming"]')
    assert delivery.cadence(weekly) == "weekly" and delivery.describe(weekly) == "Every Monday at 07:00"


def test_set_up_from_email_reports_and_sent_to_the_team(client, db, two_tenants):
    alpha, bravo = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    _configure(client)
    team = delivery.add_contact(db, None, "Me", "me@msp.com")
    outsider = delivery.add_contact(db, alpha, "Client", "it@alpha.com")
    now = datetime.now(UTC)
    _finding(db, alpha, "mfa_registration", "critical", now - timedelta(minutes=30))
    _failed_run(db, bravo, now - timedelta(minutes=20))

    assert "Set up the morning email" in client.get("/emails").get_data(as_text=True)
    form = client.get("/emails/today/new").get_data(as_text=True)
    assert "Every weekday, Monday to Friday." in form and 'name="day_of_week"' not in form
    resp = client.post("/emails/today/new", data={"frequency": "weekly", "hour": "7", "contacts": [team, outsider]})
    assert resp.status_code == 302
    s = db.execute("SELECT * FROM email_schedules WHERE kind = 'digest'").fetchone()
    assert json.loads(s["reports"]) == ["today"] and s["frequency"] == "weekly" and not s["include_xlsx"]
    assert len(s["last_period"]) == 10  # a day: saving never sends straight away
    assert [c["email"] for c in delivery.recipients(db, "schedule", s["id"])] == ["me@msp.com"]
    overview = client.get("/emails").get_data(as_text=True)
    assert "Today&#39;s ledger · every client" in overview and "Every weekday at 07:00" in overview
    assert "Set up the morning email" not in overview
    assert client.get(f"/schedules/{s['id']}/preview").status_code == 200
    assert "Alpha Ltd" in client.get(f"/schedules/{s['id']}/preview/body").get_data(as_text=True)

    client.post(f"/schedules/{s['id']}/send")
    ((_, _, msg, to_addrs),) = sends()
    assert to_addrs == ["me@msp.com"]
    assert msg["Subject"].startswith("Today's ledger · ") and msg["Subject"].endswith(": 2 things need someone")
    html = msg.get_body(("html",)).get_content()
    assert "2 things need someone today: 1 new critical finding and 1 failed collection." in html
    assert f"/tenants/{alpha}/findings#check-mfa_registration" in html and "Bravo Inc" in html
    assert "Open Today" in html and "/today" in html
    text = msg.get_body(("plain",)).get_content()
    assert "Critical · Alpha Ltd" in text and "Failed · Bravo Inc: 1 collection failed. Users" in text
    assert "Open Today: " in text
    row = db.execute("SELECT * FROM email_deliveries ORDER BY id DESC").fetchone()
    assert row["kind"] == "digest" and row["status"] == "sent"

    # Editing keeps it Today's ledger, every weekday.
    client.post(f"/schedules/{s['id']}/edit", data={"frequency": "weekly", "hour": "8", "contacts": [team]})
    s = db.execute("SELECT * FROM email_schedules WHERE id = ?", (s["id"],)).fetchone()
    assert delivery.is_ledger(s) and delivery.describe(s) == "Every weekday at 08:00"


def test_goes_out_each_weekday_and_mondays_covers_the_weekend(db, two_tenants, app, cfg, vault):
    alpha, bravo = two_tenants
    for key, value in {"smtp_host": "smtp.example.com", "smtp_from": "r@msp.com", "smtp_security": "none"}.items():
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
    team = delivery.add_contact(db, None, "Me", "me@msp.com")
    sid = db.execute(
        "INSERT INTO email_schedules (kind, reports, include_xlsx, frequency, day_of_week, hour, timezone,"
        " last_period, created_at) VALUES ('digest', '[\"today\"]', 0, 'weekly', 0, 7, 'UTC', '2026-10-23', ?)",
        (utcnow(),),
    ).lastrowid
    delivery.set_recipients(db, "schedule", sid, [team])
    assert delivery.tick(app, db, vault, cfg, datetime(2026, 10, 24, 9, tzinfo=UTC)) == 0  # Saturday

    _finding(db, alpha, "mfa_registration", "critical", datetime(2026, 10, 24, 10, tzinfo=UTC))  # on Saturday
    _finding(db, bravo, "external_forwarding", "high", datetime(2026, 10, 22, 10, tzinfo=UTC))  # before Friday's
    monday = datetime(2026, 10, 26, 7, 5, tzinfo=UTC)
    assert delivery.tick(app, db, vault, cfg, monday) == 1
    ((_, _, msg, _),) = sends()
    assert msg["Subject"] == "Today's ledger · Mon 26 Oct: 1 thing needs someone"
    html = msg.get_body(("html",)).get_content()
    since_friday, still_open = html.index("Since Friday"), html.index("still open")
    assert since_friday < html.index("Alpha Ltd") < still_open < html.index("Bravo Inc")
    assert "Monday 26 October 2026" in html
    assert db.execute("SELECT last_period FROM email_schedules WHERE id = ?", (sid,)).fetchone()[0] == "2026-10-26"
    assert delivery.tick(app, db, vault, cfg, monday + timedelta(hours=1)) == 0  # once a day


def test_one_group_and_only_active_clients(db, two_tenants, app):
    alpha, bravo = two_tenants
    now = datetime.now(UTC)
    _finding(db, alpha, "mfa_registration", "critical", now - timedelta(hours=1))
    _finding(db, bravo, "mfa_registration", "critical", now - timedelta(hours=1))
    gold = tenant_groups.create(db, {"name": "Gold", "category": "", "description": ""}, "test")
    tenant_groups.set_members(db, gold, [alpha])
    s = _schedule(id=1, group_id=gold, message="", timezone="UTC")
    data, group = delivery.ledger_data(db, s, "UTC", now)
    assert group["name"] == "Gold" and [e.tenant["name"] for e in data.entries] == ["Alpha Ltd"]
    db.execute("UPDATE tenants SET enabled = 0 WHERE id = ?", (alpha,))
    data, _ = delivery.ledger_data(db, s, "UTC", now)
    assert data.tenants == 0 and data.entries == []
    built = delivery.build_ledger_email(app, db, s, [], now=now)
    assert built.email.subject.endswith(": no active clients to look after yet")


def test_analyst_cannot_set_it_up(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "analyst", role="analyst", tenants=[alpha])
    login(client, "analyst")
    assert client.get("/emails/today/new").status_code == 403
