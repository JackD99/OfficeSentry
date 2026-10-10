"""Sign-in throttling that can't be turned against the person signing in.

Wrong passwords used to lock the account for everyone, so anyone who knew a
username could keep an admin locked out from anywhere. Instead, attempts are
limited by where they come from (rows in ``login_attempts``, a 15-minute
sliding window):

* **Address and name**: 5 failed passwords for one name from one address, then
  that address waits until the oldest of them is 15 minutes old. The same
  person signing in from another address isn't affected.
* **Address**: 20 failed passwords from one address, whatever the names.
* **Name, from new addresses**: once a name has 10 failures in the window from
  anywhere (a spread-out attack), addresses that haven't signed in to that
  account in the last 90 days share one attempt at a time, 5 seconds apart,
  doubling for every further 10 failures, up to 5 minutes. Addresses the
  person has signed in from before are never slowed.

An attempt is recorded (``pending``) in the same write transaction as the
check, before the password is hashed, so parallel requests can't get past a
limit. Unknown names are counted exactly like real ones, so the limits don't
reveal which names exist. Two-step codes are limited per account instead
(``users.failed_logins``): only someone with the password can reach that step.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .db import transaction

WINDOW = timedelta(minutes=15)
PER_ADDRESS_AND_NAME = 5
PER_ADDRESS = 20
NAME_SOFT_LIMIT = 10
SPACING_START = timedelta(seconds=5)
SPACING_MAX = timedelta(minutes=5)
KNOWN_FOR = timedelta(days=90)  # how long a successful sign-in makes an address "known" for that account
KEEP_FAILED = timedelta(days=2)


@dataclass
class Refused:
    retry_after: int  # seconds
    reason: str  # "address" (this address, this name or any name) or "name" (spread-out attack on the name)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def name_key(username: str) -> str:
    return (username or "").strip().lower()[:64]


def _wait_for(conn, where: str, args: tuple, since: str, limit: int, now: datetime) -> int | None:
    """Seconds until fewer than ``limit`` failures match ``where`` in the window, or None if already fewer."""
    rows = conn.execute(
        f"SELECT ts FROM login_attempts WHERE outcome != 'ok' AND ts >= ? AND {where} ORDER BY ts DESC LIMIT ?",
        (since, *args, limit),
    ).fetchall()
    if len(rows) < limit:
        return None
    # The limit-th newest failure has to leave the window first.
    return max(1, int((_parse(rows[-1]["ts"]) + WINDOW - now).total_seconds()) + 1)


def known_address(conn, user_id: int | None, ip: str, now: datetime | None = None) -> bool:
    if user_id is None:
        return False
    since = _iso((now or datetime.now(UTC)) - KNOWN_FOR)
    return (
        conn.execute(
            "SELECT 1 FROM login_attempts WHERE user_id = ? AND ip = ? AND outcome = 'ok' AND ts >= ? LIMIT 1",
            (user_id, ip, since),
        ).fetchone()
        is not None
    )


def reserve(
    conn: sqlite3.Connection, ip: str, username: str, user_id: int | None, now: datetime | None = None
) -> int | Refused:
    """Record a password attempt before it is checked. Returns the attempt's id, or why it is refused."""
    now = now or datetime.now(UTC)
    name = name_key(username)
    since = _iso(now - WINDOW)
    with transaction(conn, immediate=True):  # one writer at a time: the checks and the insert are one step
        for where, args, limit in (
            ("ip = ? AND username = ?", (ip, name), PER_ADDRESS_AND_NAME),
            ("ip = ?", (ip,), PER_ADDRESS),
        ):
            if (wait := _wait_for(conn, where, args, since, limit, now)) is not None:
                return Refused(wait, "address")
        slowed = 0
        failures = conn.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE outcome != 'ok' AND ts >= ? AND username = ?", (since, name)
        ).fetchone()[0]
        if failures >= NAME_SOFT_LIMIT and not known_address(conn, user_id, ip, now):
            spacing = min(SPACING_MAX, SPACING_START * 2 ** ((failures - NAME_SOFT_LIMIT) // NAME_SOFT_LIMIT))
            last = conn.execute(
                "SELECT MAX(ts) FROM login_attempts WHERE username = ? AND slowed = 1 AND ts >= ?", (name, since)
            ).fetchone()[0]
            if last and now - _parse(last) < spacing:
                return Refused(max(1, int((_parse(last) + spacing - now).total_seconds()) + 1), "name")
            slowed = 1
        return conn.execute(
            "INSERT INTO login_attempts (ts, ip, username, user_id, slowed) VALUES (?, ?, ?, ?, ?)",
            (_iso(now), ip, name, user_id, slowed),
        ).lastrowid


def finish(conn: sqlite3.Connection, attempt_id: int, ok: bool) -> None:
    conn.execute("UPDATE login_attempts SET outcome = ? WHERE id = ?", ("ok" if ok else "failed", attempt_id))


def recent_failures(conn: sqlite3.Connection, username: str, now: datetime | None = None) -> tuple[int, int]:
    """(failed passwords, addresses they came from) for a name in the current window."""
    since = _iso((now or datetime.now(UTC)) - WINDOW)
    row = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT ip) FROM login_attempts WHERE outcome != 'ok' AND ts >= ? AND username = ?",
        (since, name_key(username)),
    ).fetchone()
    return row[0], row[1]


def clear(conn: sqlite3.Connection, username: str) -> int:
    """Forget a name's failed attempts (an admin's "Unlock now")."""
    return conn.execute(
        "DELETE FROM login_attempts WHERE username = ? AND outcome != 'ok'", (name_key(username),)
    ).rowcount


def prune(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """Drop failed attempts after two days and successful ones after KNOWN_FOR. Run daily by the worker."""
    now = now or datetime.now(UTC)
    return conn.execute(
        "DELETE FROM login_attempts WHERE (outcome != 'ok' AND ts < ?) OR ts < ?",
        (_iso(now - KEEP_FAILED), _iso(now - KNOWN_FOR)),
    ).rowcount
