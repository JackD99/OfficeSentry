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


def test_a_quiet_today_claims_only_what_the_person_sees():
    since = datetime.now(UTC) - timedelta(days=1)
    no_collect = home.Today([], 2, since, sees=frozenset({"finding", "fixed", "due"}))
    assert (
        no_collect.sentence() == "Nothing needs you today: no new critical or high findings and nothing due this week."
    )
    assert no_collect.all_clear == "No new critical or high findings and nothing is due this week."
    assert no_collect.what_is_new == "what the night's collection found and what's due this week"
    no_findings = home.Today([], 1, since, sees=frozenset({"collection", "access"}))
    assert no_findings.sentence() == "Nothing needs you today. Your client is collecting."
    assert no_findings.all_clear == "Every collection is working."
    assert no_findings.what_is_new == "collections that failed"
    nothing = home.Today([], 2, since, sees=frozenset())
    assert "doesn't include any of them" in nothing.sentence()
    assert nothing.all_clear == "" and nothing.what_is_new == ""


def test_today_shows_a_first_line_role_no_figures_it_cannot_see(client, db, two_tenants):
    # First line opens people, identity, email and device reports: no findings, collections or due items, so
    # Today mustn't say there are no critical findings or nothing due, nor show those figures as 0.
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "fl", role="analyst", tenants=[alpha])
    db.execute("UPDATE users SET role_id = (SELECT id FROM roles WHERE key = 'first_line') WHERE username = 'fl'")
    login(client, "fl")
    page = client.get("/today").get_data(as_text=True)
    assert "no new critical or high findings" not in page.lower()
    assert "New critical and high" not in page and "Due this week" not in page and "Collections failing" not in page
    assert "Nothing to show" in page and "New since" not in page


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


# -- Since you last looked, Hand to and Snooze -----------------------------------------------------------


def _failed_run(db, tenant_id, age=timedelta(hours=1)):
    at = (datetime.now(UTC) - age).strftime(ISO)
    db.execute(
        "INSERT INTO runs (tenant_id, collector, status, trigger, created_at, finished_at, error)"
        " VALUES (?, 'users', 'failed', 'schedule', ?, ?, 'Graph 500')",
        (tenant_id, at, at),
    )


def _audits(db, action):
    return [r["detail"] for r in db.execute("SELECT detail FROM audit_log WHERE action = ?", (action,))]


def test_the_line_falls_where_the_person_last_looked():
    now = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
    # The first visit shows the last day, and keeps when it was opened and where the line was.
    assert home.since_for({}, now) == (
        now - timedelta(days=1),
        "yesterday",
        ("2026-10-17T09:00:00Z", "2026-10-16T09:00:00Z"),
    )
    # Opening it again within a couple of hours keeps the line where it was.
    back = {"today_opened_at": "2026-10-17T08:00:00Z", "today_since": "2026-10-16T17:30:00Z"}
    assert home.since_for(back, now)[:2] == (datetime(2026, 10, 16, 17, 30, tzinfo=UTC), "you last looked")
    # Coming back later moves it to the last visit, and never further back than a week.
    back["today_opened_at"] = "2026-10-17T06:00:00Z"
    assert home.since_for(back, now)[0] == datetime(2026, 10, 17, 6, 0, tzinfo=UTC)
    away = {"today_opened_at": "2026-09-01T09:00:00Z", "today_since": "2026-08-31T09:00:00Z"}
    assert home.since_for(away, now)[0] == now - home.LOOKBACK


def test_today_page_remembers_the_last_visit(client, db, two_tenants):
    alpha, _ = two_tenants
    uid = make_user(db, "admin")
    login(client, "admin")
    assert "New since yesterday" in client.get("/today").get_data(as_text=True)
    row = db.execute("SELECT today_opened_at, today_since FROM users WHERE id = ?", (uid,)).fetchone()
    assert row["today_since"] < row["today_opened_at"]
    db.execute(
        "UPDATE users SET today_opened_at = ? WHERE id = ?",
        ((datetime.now(UTC) - timedelta(hours=5)).strftime(ISO), uid),
    )
    _finding(db, alpha, "mfa_registration", "high", age=timedelta(hours=6))  # before the last visit
    _finding(db, alpha, "external_forwarding", "critical", age=timedelta(hours=1))
    page = client.get("/today").get_data(as_text=True)
    assert "New since you last looked" in page and "Since you last looked" in page
    assert page.index('class="sevtext critical"') < page.index("Seen before") < page.index('class="ledger-line seen"')


def test_hand_a_finding_line_to_a_colleague(client, db, two_tenants):
    alpha, bravo = two_tenants
    _finding(db, alpha, "mfa_registration", "high")
    _finding(db, alpha, "mfa_registration", "high", age=timedelta(hours=3))  # the same line
    _finding(db, bravo, "mfa_registration", "high")
    mine = [r["id"] for r in db.execute("SELECT id FROM findings WHERE tenant_id = ? ORDER BY id", (alpha,))]
    other = db.execute("SELECT id FROM findings WHERE tenant_id = ?", (bravo,)).fetchone()["id"]
    make_user(db, "admin")
    ana = make_user(db, "ana", role="analyst", tenants=[alpha])
    bob = make_user(db, "bob", role="analyst", tenants=[bravo])
    login(client, "admin")
    page = client.get("/today").get_data(as_text=True)
    menu = page.split(f'action="/tenants/{alpha}/today/hand"')[1].split("</form>")[0]
    assert f'value="{ana}"' in menu and f'value="{bob}"' not in menu and "(me)" in menu
    assert all(f'name="finding" value="{i}"' in menu for i in mine)
    form = {"key": "finding:mfa_registration", "finding": [str(i) for i in mine]}
    # Someone who can't see the client is refused, and so are another client's findings.
    client.post(f"/tenants/{alpha}/today/hand", data=form | {"to": str(bob)})
    resp = client.post(f"/tenants/{alpha}/today/hand", data=form | {"finding": str(other), "to": str(ana)})
    assert resp.status_code == 404
    assert {r[0] for r in db.execute("SELECT handed_to FROM findings")} == {None}
    resp = client.post(f"/tenants/{alpha}/today/hand", data=form | {"to": str(ana)})
    assert resp.headers["Location"] == "/today#ledger"
    assert [r[0] for r in db.execute("SELECT handed_to FROM findings ORDER BY id")] == [ana, ana, None]
    assert _audits(db, "finding_handed") == ["finding:mfa_registration (2 findings) to ana"]
    assert "Handed to ana" in client.get("/today").get_data(as_text=True)
    client.post("/logout")
    login(client, "ana")
    assert "Handed to you" in client.get("/today").get_data(as_text=True)


def test_lines_handed_to_you_are_kept_together_at_the_top(client, db, two_tenants):
    alpha, bravo = two_tenants
    _finding(db, alpha, "mfa_registration", "high")
    _finding(db, bravo, "external_forwarding", "critical")
    _failed_run(db, bravo)
    make_user(db, "admin")
    ana = make_user(db, "ana", role="analyst", tenants=[alpha, bravo])
    found = [str(r["id"]) for r in db.execute("SELECT id FROM findings WHERE tenant_id = ?", (alpha,))]
    login(client, "admin")
    client.post(f"/tenants/{alpha}/today/hand", data={"key": "finding:mfa_registration", "finding": found, "to": ana})
    client.post(f"/tenants/{bravo}/today/hand", data={"key": "collection", "to": str(ana)})
    page = client.get("/today").get_data(as_text=True)
    assert 'id="handed"' not in page and page.count("Handed to ana</div>") == 2  # nothing is handed to admin
    data = home.today(db, db.execute("SELECT * FROM tenants").fetchall())
    held = data.handed_to(ana)
    assert {(e.kind, e.handed_by) for e in held} == {("finding", "admin"), ("collection", "admin")}
    assert data.handed_to(None) == [] and len(data.entries) == 3 and data.new_severe == 2  # still counted
    client.post("/logout")
    login(client, "ana")
    page = client.get("/today").get_data(as_text=True)
    handed, ledger = page.split('id="handed"')[1].split('id="ledger"')
    assert handed.count('class="ledger-line') == 2 and handed.count("Handed to you by admin") == 2
    assert ledger.count('class="ledger-line') == 1 and 'data-kind="finding"' in ledger and "Handed" not in ledger
    # Everything handed to them: no ledger below, only the group.
    client.post(
        f"/tenants/{bravo}/today/hand",
        data={
            "key": "finding:external_forwarding",
            "to": str(ana),
            "finding": [str(r["id"]) for r in db.execute("SELECT id FROM findings WHERE tenant_id = ?", (bravo,))],
        },
    )
    page = client.get("/today").get_data(as_text=True)
    assert 'id="handed"' in page and 'id="ledger"' not in page
    # Taking a line yourself doesn't say you handed it to yourself.
    assert "Handed to you by ana" not in page


def test_snooze_a_failed_collection_and_bring_it_back(client, db, two_tenants):
    alpha, bravo = two_tenants
    _failed_run(db, bravo)
    make_user(db, "admin")
    login(client, "admin")
    client.post(f"/tenants/{bravo}/today/snooze", data={"key": "collection", "until": "tomorrow"})
    row = db.execute("SELECT * FROM item_state").fetchone()
    assert (row["tenant_id"], row["item_key"], row["snoozed_by"]) == (bravo, "collection", "admin")
    assert row["snoozed_until"] > datetime.now(UTC).strftime(ISO)
    assert _audits(db, "item_snoozed") == [f"collection until {row['snoozed_until']}"]
    data = home.today(db, db.execute("SELECT * FROM tenants").fetchall())
    assert data.entries == [] and [e.kind for e in data.snoozed] == ["collection"] and data.failing == 0
    page = client.get("/today").get_data(as_text=True)
    assert 'id="snoozed"' in page and "Snoozed until" in page and "Back in the list" in page
    client.post(f"/tenants/{bravo}/today/snooze", data={"key": "collection", "until": ""})
    assert db.execute("SELECT snoozed_until FROM item_state").fetchone()[0] is None
    data = home.today(db, db.execute("SELECT * FROM tenants").fetchall())
    assert [e.kind for e in data.entries] == ["collection"] and data.failing == 1
    # A snooze ends by itself.
    db.execute("UPDATE item_state SET snoozed_until = '2026-01-01T06:00:00Z'")
    assert home.today(db, db.execute("SELECT * FROM tenants").fetchall()).snoozed == []


def test_a_line_that_cleared_forgets_who_held_it(client, db, two_tenants):
    alpha, bravo = two_tenants
    _failed_run(db, bravo)
    uid = make_user(db, "admin")
    login(client, "admin")
    client.post(f"/tenants/{bravo}/today/hand", data={"key": "collection", "to": str(uid)})
    held = db.execute("SELECT * FROM item_state").fetchone()
    assert (held["handed_to"], held["handed_by"]) == (uid, "admin")
    assert "Handed to you" in client.get("/today").get_data(as_text=True)
    # Alpha's collection never failed: someone who sees collections there clears its row; nobody else does.
    db.execute("INSERT INTO item_state (tenant_id, item_key, handed_to) VALUES (?, 'collection', ?)", (alpha, uid))
    tenants = db.execute("SELECT * FROM tenants").fetchall()
    assert home.today(db, tenants).cleared == [(alpha, "collection")]
    assert home.today(db, tenants, shows=lambda kind, tenant_id: kind != "collection").cleared == []
    client.get("/today")
    assert [r["tenant_id"] for r in db.execute("SELECT tenant_id FROM item_state")] == [bravo]


def test_hand_and_snooze_need_findings_manage(client, db, two_tenants):
    from tests.test_roles import role_user

    alpha, _ = two_tenants
    _finding(db, alpha)
    _failed_run(db, alpha)
    role_user(db, "aud", "auditor", tenants=[alpha])
    login(client, "aud")
    page = client.get("/today").get_data(as_text=True)
    assert 'data-kind="finding"' in page and "line-actions" not in page
    finding_id = db.execute("SELECT id FROM findings").fetchone()["id"]
    form = {"key": "finding:mfa_registration", "finding": str(finding_id), "to": "", "until": "week"}
    assert client.post(f"/tenants/{alpha}/today/hand", data=form).status_code == 403
    assert client.post(f"/tenants/{alpha}/today/snooze", data=form).status_code == 403
    assert db.execute("SELECT snoozed_until FROM findings").fetchone()[0] is None


def test_snooze_ends_tomorrow_morning_in_the_msp_timezone():
    from zoneinfo import ZoneInfo

    from officesentry.web.home import snooze_until

    now = datetime(2026, 10, 16, 22, 30, tzinfo=UTC)  # 23:30 in London
    assert snooze_until("tomorrow", now, ZoneInfo("Europe/London")) == "2026-10-17T05:00:00Z"  # 06:00 BST
    assert snooze_until("week", now, ZoneInfo("Europe/London")) == "2026-10-23T22:30:00Z"
    assert snooze_until("", now, ZoneInfo("UTC")) is None
