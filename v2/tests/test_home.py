"""Home pages by role (home.py, web/home.py): Today, Clients, the Home link and the choice in Account settings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from officesentry import home
from tests.conftest import PASSWORD, login, make_user
from tests.seed import seed_tenant

ISO = "%Y-%m-%dT%H:%M:%SZ"


def _finding(db, tenant_id, rule_key="mfa_registration", severity="high", age=timedelta(hours=2), **extra):
    seen = (datetime.now(UTC) - age).strftime(ISO)
    db.execute(
        "INSERT INTO findings (tenant_id, rule_key, object_id, severity, subject, detail, first_seen, last_seen,"
        " accepted_at, accepted_until) VALUES (?, ?, ?, ?, 'someone', '', ?, ?, ?, ?)",
        (
            tenant_id,
            rule_key,
            f"obj-{rule_key}-{severity}-{age}",
            severity,
            seen,
            seen,
            extra.get("accepted_at"),
            extra.get("accepted_until"),
        ),
    )


def _entry(kind, level, word, tenant="A", new=True, count=1, when=None):
    return home.Entry(
        kind, level, word, when or datetime.now(UTC), {"id": 1, "name": tenant}, "x", count=count, new=new
    )


def test_today_sentence_leads_with_the_answer():
    since = datetime.now(UTC) - timedelta(days=1)
    quiet = home.Today([], 12, since)
    assert quiet.sentence() == (
        "Nothing needs you today. All 12 clients are collecting, with no new critical or high"
        " findings and nothing due this week."
    )
    busy = home.Today(
        [
            _entry("finding", "critical", "Critical"),
            _entry("finding", "high", "High", "B"),
            _entry("collection", "bad", "Failed", "C"),
            _entry("due", "warn", "This week", "C", count=4),
            _entry("fixed", "ok", "Fixed", "B", count=2),
        ],
        12,
        since,
        new_severe=2,
        new_critical=1,
        failing=1,
        due=4,
        fixed=2,
    )
    assert busy.sentence() == (
        "7 things need someone today: 2 new critical or high findings, 1 failed collection and 4 things due this"
        " week. 2 findings were fixed."
    )
    one = home.Today([_entry("finding", "high", "High")], 1, since, new_severe=1)
    assert one.sentence() == "1 thing needs someone today: 1 new high finding."
    # Nothing new, but something seen before is still open: say so rather than "nothing needs you".
    seen = home.Today([_entry("finding", "high", "High", new=False)], 3, since)
    assert seen.sentence() == "Nothing new since yesterday. 1 thing you'd seen is still open."
    assert home.Today([], 0, since).sentence() == "No active clients to look after yet."


def test_today_is_a_ledger_of_severe_findings_new_above_the_line(db, two_tenants):
    alpha, bravo = two_tenants
    _finding(db, alpha, "mfa_registration", "critical")
    _finding(db, alpha, "external_forwarding", "high")
    _finding(db, alpha, "external_forwarding", "high", age=timedelta(hours=3))  # the same check: one line
    _finding(db, alpha, "old_one", "high", age=timedelta(days=3))  # seen before: below the line
    _finding(db, alpha, "older", "high", age=timedelta(days=9))  # beyond the week: not listed
    _finding(db, alpha, "low_one", "medium")  # not severe
    _finding(db, bravo, "accepted", "high", accepted_at="2026-01-01T00:00:00Z")  # an accepted risk
    tenants = db.execute("SELECT * FROM tenants").fetchall()
    data = home.today(db, tenants)
    assert [(e.kind, e.level, e.new, e.count) for e in data.entries] == [
        ("finding", "critical", True, 1),
        ("finding", "high", True, 2),
        ("finding", "high", False, 1),
    ]
    assert data.new_severe == 3 and data.new_critical == 1 and data.clients == 1 and data.fine == 1
    critical, high, old = data.entries
    assert critical.word == "Critical" and critical.tone == "bad" and critical.key == "finding:mfa_registration"
    assert critical.detail == "someone" and critical.anchor == "#check-mfa_registration"
    assert high.detail == "2 findings: someone · someone" and old.text == "old_one"
    assert data.sentence() == "3 things need someone today: 3 new critical or high findings."


def test_today_lists_fixed_findings_and_failed_collections_with_their_times(db, two_tenants):
    alpha, bravo = two_tenants
    now = datetime.now(UTC).replace(microsecond=0)
    _finding(db, alpha, "admin_mfa", "high", age=timedelta(hours=30))
    db.execute(
        "UPDATE findings SET resolved_at = ? WHERE tenant_id = ?", ((now - timedelta(hours=2)).strftime(ISO), alpha)
    )
    _finding(db, alpha, "fixed_long_ago", "critical", age=timedelta(days=4))
    db.execute(
        "UPDATE findings SET resolved_at = ? WHERE rule_key = 'fixed_long_ago'",
        ((now - timedelta(days=2)).strftime(ISO),),
    )
    db.execute(
        "INSERT INTO runs (tenant_id, collector, status, trigger, created_at, finished_at, error)"
        " VALUES (?, 'users', 'failed', 'schedule', ?, ?, 'Graph 500')",
        (bravo, (now - timedelta(hours=1)).strftime(ISO), (now - timedelta(hours=1)).strftime(ISO)),
    )
    data = home.today(db, db.execute("SELECT * FROM tenants").fetchall(), now)
    assert [(e.kind, e.level, e.word, e.tenant["id"], e.new) for e in data.entries] == [
        ("collection", "bad", "Failed", bravo, True),
        ("fixed", "ok", "Fixed", alpha, True),
    ]
    failed, fixed = data.entries
    assert failed.text == "1 collection failed" and failed.detail == "Users"
    assert failed.when == now - timedelta(hours=1) and failed.key == "collection"
    assert fixed.text == "1 finding no longer seen" and fixed.detail == "Every admin has MFA registered (someone)"
    assert fixed.when == now - timedelta(hours=2)
    assert data.fixed == 1 and data.failing == 1 and data.clients == 1
    assert data.sentence() == "1 thing needs someone today: 1 failed collection. 1 finding was fixed."


def test_today_page_shows_only_granted_tenants(client, db, two_tenants):
    alpha, bravo = two_tenants
    _finding(db, alpha)
    _finding(db, bravo)
    make_user(db, "ana", role="analyst", tenants=[bravo])
    login(client, "ana")
    page = client.get("/today").get_data(as_text=True)
    assert "1 thing needs someone today: 1 new high finding." in page
    assert "Bravo Inc" in page and "Alpha Ltd" not in page
    assert f'href="/tenants/{bravo}/findings#check-mfa_registration"' in page
    assert page.count('data-kind="finding"') == 1 and "Seen before" not in page


def test_today_page_draws_the_line_between_new_and_seen(client, db, two_tenants):
    alpha, _ = two_tenants
    _finding(db, alpha, "mfa_registration", "critical", age=timedelta(hours=1))
    _finding(db, alpha, "external_forwarding", "high", age=timedelta(days=2))
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/today").get_data(as_text=True)
    assert "Seen before" in page and "still open" in page
    assert page.index('data-kind="finding"') < page.index("Seen before") < page.index('class="ledger-line seen"')
    assert '<time class="when"' in page and 'class="sevtext critical">Critical</span>' in page


def test_waiting_for_consent_is_on_today(db, two_tenants):
    alpha, _ = two_tenants
    db.execute("UPDATE tenants SET consented_at = NULL WHERE id = ?", (alpha,))
    data = home.today(db, db.execute("SELECT * FROM tenants").fetchall())
    assert data.waiting == 1 and data.entries[0].kind == "access" and data.entries[0].word == "Waiting"
    assert "1 client waiting for access" in data.sentence()


def test_ledger_stamps_read_in_the_msp_timezone():
    from zoneinfo import ZoneInfo

    from officesentry.web.home import stamp

    london = ZoneInfo("Europe/London")
    now = datetime(2026, 10, 16, 8, 0, tzinfo=UTC)  # 09:00 in London
    assert stamp(datetime(2026, 10, 16, 5, 41, tzinfo=UTC), now, london) == ("06:41", "Fri 16 Oct 2026, 06:41 BST")
    assert stamp(datetime(2026, 10, 15, 16, 40, tzinfo=UTC), now, london)[0] == "Thu"
    assert stamp(datetime(2026, 10, 9, 23, 30, tzinfo=UTC), now, london)[0] == "Sat"  # six days ago in London
    assert stamp(datetime(2026, 10, 8, 23, 30, tzinfo=UTC), now, london)[0] == "9 Oct"  # a week ago: the date


def test_clients_page_with_data(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    resp = client.get("/clients")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert 'aria-current="page">Accounts</a>' in page and "Alpha Ltd" in page and "Bravo Inc" in page
    assert "renewal" in page and 'id="client-table"' in page


def test_clients_sentence():
    rows = [
        home.ClientRow({"name": "A"}, 1240.0, None, 2, "draft", False, 3, 1, 50.0),
        home.ClientRow({"name": "B"}, 0.0, None, 0, "sent", False, 0, 0, None),
    ]
    data = home.Clients(rows, "GBP", "2026-10", "October 2026", 2)
    assert data.sentence(lambda v: f"£{v:,.0f}") == (
        "£1,240 a month of licence waste across 1 client; 2 renewals in"
        " the next 30 days; 1 report to approve for October 2026."
    )
    unpriced = home.Clients(
        [home.ClientRow({"name": "A"}, None, None, 0, None, False, 0, 0, None)], "GBP", "2026-10", "October 2026", 0
    )
    assert unpriced.sentence(str) == (
        "Licence waste isn't counted until licence prices are set; no renewals in the next 30 days."
    )


@pytest.mark.parametrize("path", ["/today", "/clients"])
def test_clients_cannot_open_staff_homes(client, db, two_tenants, path):
    alpha, _ = two_tenants
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    assert client.get(path).status_code == 403
    assert client.get("/home").headers["Location"] == "/"
    assert client.post("/account/home", data={"home": "clients"}).status_code == 403


def test_home_choice_and_landing(client, db, two_tenants):
    make_user(db, "admin")
    assert login(client, "admin").headers["Location"] == "/today"  # the default home
    assert client.get("/home").headers["Location"] == "/today"
    page = client.get("/account").get_data(as_text=True)
    assert 'name="home" value="today" checked' in page
    resp = client.post("/account/home", data={"home": "clients", "next": "/clients"})
    assert resp.headers["Location"] == "/clients"
    assert db.execute("SELECT home FROM users WHERE username = 'admin'").fetchone()[0] == "clients"
    assert client.get("/home").headers["Location"] == "/clients"
    assert "Your home page" in client.get("/clients").get_data(as_text=True)
    assert "Make this my home page" in client.get("/today").get_data(as_text=True)
    assert client.post("/account/home", data={"home": "admin"}).status_code == 400
    client.post("/logout")
    assert login(client, "admin").headers["Location"] == "/clients"
    client.post("/logout")
    # Asking for a page still goes there.
    resp = client.post("/login?next=/reports", data={"username": "admin", "password": PASSWORD})
    assert resp.headers["Location"] == "/reports"


def test_sidebar_home_link(client, db, two_tenants):
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/today").get_data(as_text=True)
    assert '<a href="/home" class="active" aria-current="page">' in page
    assert 'class="brand" href="/home"' in page
