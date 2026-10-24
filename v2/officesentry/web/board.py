"""The office board (``board.py``): every client on one dark, full-screen page for the office TV, worst first,
reloading itself every few minutes.

Staff who compare clients open it (as they do Today), over the clients they see narrowed to their chosen group, so
a board for one team is that team's group picked once on the TV's session. Each part of a stamp and each tile
shows only what the person may see on that client, by the same rules as Today's lines. The page has no menu and
its own dark look whatever the person chose, and works without script; with script the clock ticks and, when
there are more clients than fit on the screen, the quiet ones take turns in the space left.
"""

from __future__ import annotations

from datetime import UTC, datetime

from flask import Blueprint, g, render_template

from .. import board
from ..formatting import strftime as datefmt
from .access import can, needs
from .groups import group_aware, scoped_tenants
from .home import LINE_NEEDS, _zone, stamp

bp = Blueprint("board", __name__)


@bp.get("/board")
@needs("clients.compare", kind="staff")
@group_aware
def index():
    now = datetime.now(UTC)
    zone = _zone()
    built = board.build(
        g.db,
        scoped_tenants(),
        now,
        shows=lambda kind, tenant_id: can(LINE_NEEDS[kind], tenant_id),
        scores=lambda tenant_id: can("reports.tenant", tenant_id),
        since=lambda when: "oldest since " + stamp(when, now, zone)[0],
    )
    local = now.astimezone(zone)
    return render_template(
        "board.html",
        board=built,
        day=datefmt(local, "%A %-d %B"),
        clock=local.strftime("%H:%M"),
        zone=zone.key,
        refresh=board.REFRESH,
    )
