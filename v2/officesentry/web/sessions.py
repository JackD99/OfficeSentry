"""Server-side sessions.

The signed Flask cookie only carries a random token. The ``sessions`` table holds
its SHA-256 hash, so deleting a row signs that browser out at once (sign-out,
"sign out everywhere", an admin resetting someone's access), and a copied cookie
is useless after sign-out.

A session ends when it is deleted, after ``session_idle_minutes`` without a
request, after ``session_hours`` in total, or when the user's
``session_version`` changes (password, role, access or two-step changes).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from flask import current_app, g, request, session

from ..db import try_write

TOUCH_EVERY = timedelta(minutes=1)  # don't write on every request


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _cfg():
    return current_app.extensions["officesentry.config"]


def start(user) -> None:
    """Sign ``user`` in on this browser. Clears any earlier session state first."""
    cfg = _cfg()
    session.clear()
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    g.db.execute(
        "INSERT INTO sessions (token_hash, user_id, session_version, created_at, last_seen_at, expires_at,"
        " ip, user_agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _hash(token),
            user["id"],
            user["session_version"],
            _iso(now),
            _iso(now),
            _iso(now + timedelta(hours=cfg.session_hours)),
            (request.remote_addr or "")[:64],
            (request.user_agent.string or "")[:300],
        ),
    )
    session.permanent = True
    session["sid"] = token
    g.db.execute("DELETE FROM sessions WHERE expires_at < ?", (_iso(now),))


def current():
    """Return ``(session_row, user_row)`` for this request, or ``(None, None)``."""
    token = session.get("sid")
    if not token:
        return None, None
    row = g.db.execute("SELECT * FROM sessions WHERE token_hash = ?", (_hash(token),)).fetchone()
    if row is None:
        session.clear()
        return None, None
    user = g.db.execute("SELECT * FROM users WHERE id = ?", (row["user_id"],)).fetchone()
    now = datetime.now(UTC)
    idle = timedelta(minutes=_cfg().session_idle_minutes)
    last_seen = _parse(row["last_seen_at"])
    if (
        user is None
        or user["disabled"]
        or user["session_version"] != row["session_version"]
        or _parse(row["expires_at"]) <= now
        or last_seen + idle <= now
    ):
        g.db.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
        session.clear()
        return None, None
    if now - last_seen >= TOUCH_EVERY:
        # Best effort: with the write lock held elsewhere for long (a restore, an erase's VACUUM), a page
        # view must neither wait for it nor fail; the next request a moment later notes the time instead,
        # well within the idle limit.
        try_write(g.db, "UPDATE sessions SET last_seen_at = ? WHERE id = ?", (_iso(now), row["id"]))
    return row, user


def end() -> None:
    """Sign this browser out."""
    token = session.get("sid")
    if token:
        g.db.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))
    session.clear()


def keep_current(user_id: int, version: int) -> None:
    """After the user's session_version changes, keep this browser signed in (others end)."""
    token = session.get("sid")
    if token:
        g.db.execute(
            "UPDATE sessions SET session_version = ? WHERE token_hash = ? AND user_id = ?",
            (version, _hash(token), user_id),
        )
    g.db.execute("DELETE FROM sessions WHERE user_id = ? AND session_version != ?", (user_id, version))


def revoke_user(user_id: int, except_current: bool = False) -> int:
    """Sign a user out everywhere (optionally not on this browser). Returns how many ended."""
    keep = _hash(session.get("sid", "")) if except_current else ""
    return g.db.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, keep)).rowcount


def revoke(session_id: int, user_id: int) -> bool:
    return g.db.execute("DELETE FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id)).rowcount > 0


def list_for(user_id: int) -> list[dict]:
    now = _iso(datetime.now(UTC))
    token = session.get("sid", "")
    current_hash = _hash(token) if token else ""
    rows = g.db.execute(
        "SELECT * FROM sessions WHERE user_id = ? AND expires_at > ? ORDER BY last_seen_at DESC",
        (user_id, now),
    ).fetchall()
    return [
        {**dict(r), "current": r["token_hash"] == current_hash, "device": describe_agent(r["user_agent"])} for r in rows
    ]


def describe_agent(ua: str | None) -> str:
    """'Edge on Windows' from a user-agent string. Good enough for a sessions list."""
    ua = ua or ""
    browser = next(
        (
            name
            for key, name in (
                ("Edg/", "Edge"),
                ("OPR/", "Opera"),
                ("Firefox/", "Firefox"),
                ("Chrome/", "Chrome"),
                ("Safari/", "Safari"),
            )
            if key in ua
        ),
        "",
    )
    system = next(
        (
            name
            for key, name in (
                ("Windows", "Windows"),
                ("iPhone", "iPhone"),
                ("iPad", "iPad"),
                ("Android", "Android"),
                ("Mac OS X", "macOS"),
                ("Linux", "Linux"),
            )
            if key in ua
        ),
        "",
    )
    if browser and system:
        return f"{browser} on {system}"
    return browser or system or "Unknown browser"
