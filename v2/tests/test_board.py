"""The office board (board.py, web/board.py): every client on one dark screen, the ones that need someone first."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from officesentry import board, store
from officesentry.reports.monthly_review import core
from tests.conftest import login, make_user
from tests.test_home import _failed_run, _finding
from tests.test_roles import role_user

ISO = "%Y-%m-%dT%H:%M:%SZ"


def _read(db, tenant_id, score=None, age=timedelta(hours=3)):
    """Every source collected a little while ago, with a Secure Score when given."""
    at = (datetime.now(UTC) - age).strftime(ISO)
    for key in core():
        db.execute(
            "INSERT INTO runs (tenant_id, collector, status, trigger, created_at, started_at, finished_at)"
            " VALUES (?, ?, 'succeeded', 'schedule', ?, ?, ?)",
            (tenant_id, key, at, at, at),
        )
    if score is not None:
        db.execute(
            "INSERT INTO metrics (tenant_id, key, value, captured_at) VALUES (?, 'secure_score.pct', ?, ?)",
            (tenant_id, score, at),
        )


def _clients(db, connection_id, *names):
    ids = [store.create_tenant(db, n, f"{n.split()[0].lower()}.onmicrosoft.com", connection_id) for n in names]
    db.execute("UPDATE tenants SET consented_at = '2026-01-01T00:00:00Z'")
    return ids


def _tenants(db):
    return db.execute("SELECT * FROM tenants ORDER BY id").fetchall()


def test_the_stamps_that_need_someone_come_first(db, connection_id):
    calm, good, crit, failed, high, waiting, quiet = _clients(
        db, connection_id, "Calm Co", "Good Co", "Crit Co", "Failed Co", "High Co", "Waiting Co", "Quiet Co"
    )
    for t in (calm, good, crit, failed, high, quiet):
        _read(db, t, score={calm: 40, good: 85, quiet: 60}.get(t))
    _finding(db, crit, "mfa_registration", "critical")  # new in the last day
    _finding(db, crit, "external_forwarding", "high")
    _failed_run(db, failed)
    _finding(db, high, "external_forwarding", "high")
    _finding(db, quiet, "old_one", "high", age=timedelta(days=3))  # open, but not new
    db.execute("UPDATE tenants SET consented_at = NULL WHERE id = ?", (waiting,))
    b = board.build(db, _tenants(db))
    by_name = {s.tenant["name"]: s for s in b.stamps}
    assert [s.tenant["name"] for s in b.stamps[:3]] == ["Crit Co", "Failed Co", "High Co"]
    assert (by_name["Crit Co"].word, by_name["Crit Co"].tone) == ("2 new", "critical")
    assert by_name["Failed Co"].word == "Not read" and by_name["Failed Co"].flagged
    assert (by_name["High Co"].word, by_name["High Co"].tone) == ("1 new", "high")
    assert by_name["Waiting Co"].word == "No access yet" and by_name["Waiting Co"].rank == board.OTHER
    # The quiet ones last, the worst Secure Score first; they show it instead of a word.
    assert [s.tenant["name"] for s in b.stamps[-3:]] == ["Calm Co", "Quiet Co", "Good Co"]
    assert all(s.quiet for s in b.stamps[-3:]) and b.quiet == 3
    assert (by_name["Calm Co"].score, by_name["Good Co"].score) == (40, 85)


def test_the_tiles_count_the_day(db, connection_id):
    a, b_, c = _clients(db, connection_id, "Alpha Ltd", "Bravo Inc", "Charlie plc")
    for t in (a, b_, c):
        _read(db, t)
    _finding(db, a, "mfa_registration", "critical")
    _finding(db, a, "external_forwarding", "high")
    _failed_run(db, b_)
    _finding(db, c, "admin_mfa", "high", age=timedelta(days=3))
    db.execute(
        "UPDATE findings SET resolved_at = ? WHERE tenant_id = ?",
        ((datetime.now(UTC) - timedelta(days=2)).strftime(ISO), c),
    )
    b = board.build(db, _tenants(db))
    tiles = {t.label: t for t in b.tiles}
    assert list(tiles) == [
        "New critical and high",
        "Read in the last 36 hours",
        "Due this week",
        "Not handed to anyone",
        "Fixed this week",
    ]
    new = tiles["New critical and high"]
    assert (new.value, new.foot, new.tone) == ("2", "Alpha Ltd", "bad")
    read = tiles["Read in the last 36 hours"]
    assert (read.value, read.foot, read.tone) == ("2 of 3", "Bravo Inc not read", "bad")
    assert (tiles["Due this week"].value, tiles["Due this week"].foot) == ("0", "nothing")
    assert tiles["Not handed to anyone"].value == "3" and tiles["Not handed to anyone"].foot.startswith("oldest since")
    fixed = tiles["Fixed this week"]
    assert (fixed.value, fixed.foot, fixed.tone) == ("1", "across 1 client", "ok")


def test_paused_and_archived_clients_are_left_off(db, connection_id):
    a, b_, c = _clients(db, connection_id, "Alpha Ltd", "Bravo Inc", "Charlie plc")
    db.execute("UPDATE tenants SET enabled = 0 WHERE id = ?", (b_,))
    db.execute("UPDATE tenants SET archived_at = '2026-01-01T00:00:00Z' WHERE id = ?", (c,))
    assert [s.tenant["id"] for s in board.build(db, _tenants(db)).stamps] == [a]


def test_a_stamp_shows_only_what_the_person_sees(db, connection_id):
    (a,) = _clients(db, connection_id, "Alpha Ltd")
    _read(db, a, score=55)
    _finding(db, a, "mfa_registration", "critical")
    b = board.build(db, _tenants(db), shows=lambda kind, tenant_id: kind != "finding" and kind != "fixed")
    (s,) = b.stamps
    assert not s.findings and s.grade is None and (s.critical, s.high) == (0, 0)
    assert s.quiet and s.score == 55  # the new critical finding isn't theirs to see
    assert [t.label for t in b.tiles] == ["Read in the last 36 hours", "Due this week"]
    hidden = board.build(db, _tenants(db), shows=lambda kind, tenant_id: False, scores=lambda tenant_id: False)
    assert hidden.stamps[0].score is None and hidden.tiles == []


# -- The page ------------------------------------------------------------------------------------------


def test_the_board_page(client, db, connection_id):
    a, b_ = _clients(db, connection_id, "Alpha Ltd", "Bravo Inc")
    _read(db, a, score=72)
    _finding(db, b_, "mfa_registration", "critical")
    make_user(db, "admin")
    login(client, "admin")
    html = client.get("/board").get_data(as_text=True)
    assert 'data-theme="dark"' in html and f'http-equiv="refresh" content="{board.REFRESH}"' in html
    assert "<title>Board · " in html and 'class="sidebar"' not in html
    assert html.index(">Bravo Inc<") < html.index(">Alpha Ltd<")  # the one with something new first
    assert ">1 new<" in html and ">72%<" in html and "board.js" in html
    assert "New critical and high" in html and "Leave the board" in html
    # The Clients page offers it.
    assert 'href="/board"' in client.get("/").get_data(as_text=True)


def test_the_board_lists_only_granted_clients(client, db, connection_id):
    a, b_ = _clients(db, connection_id, "Alpha Ltd", "Bravo Inc")
    make_user(db, "ana", role="analyst", tenants=[b_])
    login(client, "ana")
    html = client.get("/board").get_data(as_text=True)
    assert ">Bravo Inc<" in html and ">Alpha Ltd<" not in html


def test_a_role_without_findings_sees_no_counts(client, db, connection_id):
    (a,) = _clients(db, connection_id, "Alpha Ltd")
    _finding(db, a, "mfa_registration", "critical")
    role_user(db, "first", "first_line", all_tenants=True)
    login(client, "first")
    html = client.get("/board").get_data(as_text=True)
    assert ">Alpha Ltd<" in html
    assert "New critical and high" not in html and "sev-critical" not in html and "Fixed this week" not in html


def test_clients_cannot_open_the_board(client, db, connection_id):
    (a,) = _clients(db, connection_id, "Alpha Ltd")
    make_user(db, "someone", role="client", tenants=[a])
    login(client, "someone")
    assert client.get("/board").status_code == 403
