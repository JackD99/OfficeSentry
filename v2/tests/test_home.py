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
    db.execute("INSERT INTO findings (tenant_id, rule_key, object_id, severity, subject, detail, first_seen, last_seen,"
               " accepted_at, accepted_until) VALUES (?, ?, ?, ?, 'someone', '', ?, ?, ?, ?)",
               (tenant_id, rule_key, f"obj-{rule_key}-{severity}-{age}", severity, seen, seen,
                extra.get("accepted_at"), extra.get("accepted_until")))


def test_today_sentence_leads_with_the_answer():
    since = datetime.now(UTC)
    quiet = home.Today([], 12, 0, 0, 0, 0, 0, 0, since)
    assert quiet.sentence() == ("Nothing needs you today. All 12 clients are collecting, with no new critical or high"
                                " findings and nothing due this week.")
    rows = [home.TodayRow({"name": "A"}), home.TodayRow({"name": "B"}), home.TodayRow({"name": "C"})]
    busy = home.Today(rows, 12, 2, 1, 1, 0, 0, 4, since)
    assert busy.sentence() == ("3 clients need you today: 2 new critical or high findings, 1 failed collection"
                               " and 4 things due this week.")
    one = home.Today(rows[:1], 1, 1, 0, 0, 0, 0, 0, since)
    assert one.sentence() == "1 client needs you today: 1 new high finding."
    assert home.Today([], 0, 0, 0, 0, 0, 0, 0, since).sentence() == "No active clients to look after yet."


def test_today_counts_only_new_open_unaccepted_severe_findings(db, two_tenants):
    alpha, bravo = two_tenants
    _finding(db, alpha, "mfa_registration", "critical")
    _finding(db, alpha, "external_forwarding", "high")
    _finding(db, alpha, "old_one", "high", age=timedelta(days=3))  # not new
    _finding(db, alpha, "low_one", "medium")  # not severe
    _finding(db, bravo, "accepted", "high", accepted_at="2026-01-01T00:00:00Z")  # an accepted risk
    tenants = db.execute("SELECT * FROM tenants").fetchall()
    data = home.today(db, tenants)
    assert [r.tenant["id"] for r in data.rows] == [alpha]
    assert data.new_severe == 2 and data.new_critical == 1
    reason = data.rows[0].reasons[0]
    assert reason.kind == "findings" and reason.tone == "bad" and reason.text == "2 new critical or high findings"
    assert data.sentence().startswith("1 client needs you today: 2 new critical or high findings")


def test_today_page_shows_only_granted_tenants(client, db, two_tenants):
    alpha, bravo = two_tenants
    _finding(db, alpha)
    _finding(db, bravo)
    make_user(db, "ana", role="analyst", tenants=[bravo])
    login(client, "ana")
    page = client.get("/today").get_data(as_text=True)
    assert "1 client needs you today: 1 new high finding." in page
    assert "Bravo Inc" in page and "Alpha Ltd" not in page
    assert f'href="/tenants/{bravo}/findings"' in page


def test_waiting_for_consent_is_on_today(db, two_tenants):
    alpha, _ = two_tenants
    db.execute("UPDATE tenants SET consented_at = NULL WHERE id = ?", (alpha,))
    data = home.today(db, db.execute("SELECT * FROM tenants").fetchall())
    assert data.waiting == 1 and data.rows[0].reasons[0].kind == "access"
    assert "1 waiting for access" in data.sentence()


def test_clients_page_with_data(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    resp = client.get("/clients")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert 'aria-current="page">Clients</a>' in page and "Alpha Ltd" in page and "Bravo Inc" in page
    assert "renewal" in page and 'id="client-table"' in page


def test_clients_sentence():
    rows = [home.ClientRow({"name": "A"}, 1240.0, None, 2, "draft", False, 3, 1, 50.0),
            home.ClientRow({"name": "B"}, 0.0, None, 0, "sent", False, 0, 0, None)]
    data = home.Clients(rows, "GBP", "2026-10", "October 2026", 2)
    assert data.sentence(lambda v: f"£{v:,.0f}") == ("£1,240 a month of licence waste across 1 client; 2 renewals in"
                                                    " the next 30 days; 1 report to approve for October 2026.")
    unpriced = home.Clients([home.ClientRow({"name": "A"}, None, None, 0, None, False, 0, 0, None)], "GBP",
                            "2026-10", "October 2026", 0)
    assert unpriced.sentence(str) == ("Licence waste isn't counted until licence prices are set; no renewals in the"
                                      " next 30 days.")


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
