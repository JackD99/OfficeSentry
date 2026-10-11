"""A long write lock (a restore, a backup, an erase's VACUUM) must not hold up or fail page views, and a write
that still finds the database locked says the portal is busy instead of failing with a 500."""

import time
from datetime import UTC, datetime, timedelta

import pytest

from officesentry import web
from officesentry.db import connect, is_locked, try_write
from tests.conftest import login, make_user


@pytest.fixture
def hold_lock(cfg):
    """Call it to have another connection hold the write lock from then on; ``release()`` lets go."""
    conns = []

    def hold():
        other = connect(cfg.db_path)
        other.execute("BEGIN IMMEDIATE")
        conns.append(other)
        return other

    yield hold
    for other in conns:
        other.close()


def release(other) -> None:
    other.execute("ROLLBACK")
    other.execute("BEGIN")  # a reader now, which blocks no write


def _seen(db) -> str:
    return db.execute("SELECT last_seen_at FROM sessions").fetchone()[0]


def test_a_page_view_doesnt_wait_for_a_long_write_to_note_the_session(client, db, hold_lock):
    make_user(db, "admin")
    login(client, "admin")
    stale = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute("UPDATE sessions SET last_seen_at = ?", (stale,))
    locked = hold_lock()
    started = time.monotonic()
    assert client.get("/account").status_code == 200
    assert time.monotonic() - started < 3  # not the 5 seconds a page waits for a write, let alone the worker's 30
    assert _seen(db) == stale  # the touch was skipped, not failed
    release(locked)
    assert client.get("/account").status_code == 200
    assert _seen(db) > stale


def test_the_dashboard_shows_while_its_rows_cant_be_stored(client, db, two_tenants, hold_lock):
    make_user(db, "admin")
    login(client, "admin")
    locked = hold_lock()
    started = time.monotonic()
    page = client.get("/")
    assert page.status_code == 200 and b"Alpha Ltd" in page.data
    assert time.monotonic() - started < 3
    assert db.execute("SELECT COUNT(*) FROM rollup_cache").fetchone()[0] == 0
    release(locked)
    assert client.get("/").status_code == 200
    assert db.execute("SELECT COUNT(*) FROM rollup_cache").fetchone()[0] == 2  # stored once the lock cleared


def test_a_write_that_finds_the_database_locked_says_busy(client, db, hold_lock, monkeypatch):
    make_user(db, "admin")
    login(client, "admin")
    locked = hold_lock()
    monkeypatch.setattr(web, "WEB_BUSY_TIMEOUT_MS", 200)  # a page waits 5 seconds in production
    resp = client.post("/logout")
    assert resp.status_code == 503 and resp.headers["Retry-After"] == "10"
    assert b"busy right now" in resp.data and b"try again" in resp.data
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1  # nothing happened; the session stands
    release(locked)
    assert client.post("/logout").status_code == 302


def test_try_write_only_swallows_a_held_lock(db, hold_lock):
    hold_lock()
    assert not try_write(db, "UPDATE settings SET value = value WHERE key = 'none'")
    assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 30000  # the usual wait is put back
    with pytest.raises(Exception, match="no such table") as caught:
        try_write(db, "UPDATE nowhere SET x = 1")
    assert not is_locked(caught.value)
