"""Favourite reports: the detail reports a person starred, listed first in the All reports menu
(``_macros.report_picker``) and on a client's Reports tab (``_macros.report_directory``), on every client.

The star is on a detail report's page (``_favourite.html``). A favourite is a report key kept for the person
(``report_favourites``, migration 0054), not for a client, so it lists on each client where that report is there
and the person may open it. Nothing is written to a tenant.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, g, redirect, request, url_for

from ..db import transaction, utcnow
from ..reports import data_reports, library
from .access import can, login_required, safe_next
from .reports import entry_capability, hidden_reports, report_capability

bp = Blueprint("favourites", __name__)

MAX_FAVOURITES = 30  # per person


def keys() -> list[str]:
    """The signed-in person's favourite report keys, in the order they were starred (once per request)."""
    if g.get("user") is None or g.get("db") is None:
        return []
    if "favourite_keys" not in g:
        g.favourite_keys = [
            r[0]
            for r in g.db.execute(
                "SELECT report_key FROM report_favourites WHERE user_id = ? ORDER BY created_at, rowid",
                (g.user["id"],),
            )
        ]
    return g.favourite_keys


def is_favourite(key: str) -> bool:
    return key in keys()


def entries(tenant_id: int) -> list[library.Entry]:
    """The person's favourites that open on this client, as the report menus list them."""
    starred = keys()
    if not starred:
        return []
    found = {
        e.key: e
        for area in library.catalogue(hidden_reports(tenant_id))
        for e in area.entries
        if e.kind == "data" and e.key in starred
    }
    return [found[k] for k in starred if k in found and can(entry_capability(found[k]), tenant_id)]


@bp.post("/reports/<key>/favourite")
@login_required
def toggle(key: str):
    """Star a report (``on=1``) or take the star off (``on=0``), then go back to the page."""
    if key not in data_reports.REPORTS:
        abort(404)
    if not can(report_capability(key)):  # a report this person opens on no client
        abort(403)
    title = data_reports.REPORTS[key][0]
    back = safe_next(request.form.get("next"), url_for("reports.overview"))
    user_id = g.user["id"]
    if request.form.get("on") == "1":
        count = g.db.execute("SELECT COUNT(*) FROM report_favourites WHERE user_id = ?", (user_id,)).fetchone()[0]
        if count >= MAX_FAVOURITES and not is_favourite(key):
            flash(
                f"You can keep up to {MAX_FAVOURITES} favourites. Take the star off one you don't use first.", "error"
            )
            return redirect(back)
        with transaction(g.db):
            g.db.execute(
                "INSERT OR IGNORE INTO report_favourites (user_id, report_key, created_at) VALUES (?, ?, ?)",
                (user_id, key, utcnow()),
            )
        flash(f"{title} is in your favourites, first in All reports on every client.", "success")
    else:
        with transaction(g.db):
            g.db.execute("DELETE FROM report_favourites WHERE user_id = ? AND report_key = ?", (user_id, key))
        flash(f"{title} is no longer in your favourites.", "success")
    return redirect(back)
