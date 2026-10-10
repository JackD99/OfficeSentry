"""The public demo site: anyone can look around, nobody can change anything.

Every visitor without a session is the demo user, and every request that isn't a
GET, HEAD or OPTIONS is refused with a plain message. It takes two switches, so
one wrong setting can't open up a real portal:

* ``OFFICESENTRY_PUBLIC_DEMO=1`` in the environment, and
* the ``public_demo`` setting, which only ``scripts/demo_data.py --public`` writes,
  into a new database of fictional tenants.

The demo user is never an admin: an admin (or a disabled account) called "demo"
signs nobody in. Visitors get no server-side session, so the sessions table
doesn't grow with every visit, and there is nothing to sign out of.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from flask import current_app, flash, g, jsonify, redirect, request

MARKER = "public_demo"
USERNAME = "demo"
ROLES = ("analyst", "client")  # never "admin"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
NOTICE = (
    "This is the demo, so nothing can be changed here. Install Office Sentry to try it on your own clients' tenants."
)


def _cfg():
    return current_app.extensions["officesentry.config"]


def active(conn) -> bool:
    """Both switches are on: the environment setting and the demo database's own marker."""
    if not _cfg().public_demo:
        return False
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (MARKER,)).fetchone()
    return bool(row and row[0] == "1")


def visitor(conn):
    """The account every visitor uses, or None if it is missing, disabled or an admin."""
    return conn.execute(
        f"SELECT * FROM users WHERE username = ? AND disabled = 0 AND role IN ({', '.join('?' * len(ROLES))})",
        (USERNAME, *ROLES),
    ).fetchone()


def link() -> str:
    """Where the banner's "Get Office Sentry" goes: OFFICESENTRY_PUBLIC_DEMO_LINK if it's a web address."""
    value = _cfg().public_demo_link
    return value if urlsplit(value).scheme in ("http", "https") and urlsplit(value).netloc else ""


def sign_in_visitor() -> None:
    """Called by ``access.load_user``: on the demo site, a browser without a session is the demo user."""
    g.public_demo = active(g.db)
    if g.public_demo and g.get("user") is None:
        g.user = visitor(g.db)


def refuse_changes():
    """``before_request``: on the demo site, anything but reading gets the notice instead."""
    from .access import safe_next  # access imports this module

    if not g.get("public_demo") or request.method in SAFE_METHODS:
        return None
    if request.headers.get("X-Requested-With") or request.accept_mimetypes.best == "application/json":
        return jsonify(ok=False, error=NOTICE), 403
    flash(NOTICE, "warning")
    back = urlsplit(request.referrer or "")
    same_site = back.netloc == request.host
    target = back.path + (f"?{back.query}" if back.query else "") if same_site else None
    return redirect(safe_next(target))


def add_headers(resp):
    """Keep the demo's fictional pages out of search engines."""
    if g.get("public_demo"):
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp
