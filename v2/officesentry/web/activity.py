"""Settings → Activity log: the audit log for admins, with filters, paging and a CSV export."""

from __future__ import annotations

import json
from datetime import date, timedelta

from flask import Blueprint, Response, g, render_template, request

from ..db import AUDIT_NOISE, AUDIT_NOISE_DAYS, audit
from ..reports.tables import Table, to_csv
from .access import current, needs, unseen_client_accounts, visible_tenants

bp = Blueprint("activity", __name__)

PAGE_SIZE = 50
CSV_LIMIT = 100_000


def _day(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def _filters() -> dict:
    args = request.args
    return {
        "user": args.get("user", "").strip()[:64],
        "action": args.get("action", "").strip()[:64],
        "tenant": args.get("tenant", type=int),
        "from": _day(args.get("from")),
        "to": _day(args.get("to")),
    }


def _where(f: dict) -> tuple[str, list]:
    where, params = _scope()
    if f["user"]:
        where.append("a.username = ?")
        params.append(f["user"])
    if f["action"]:
        where.append("a.action = ?")
        params.append(f["action"])
    if f["tenant"] is not None:
        where.append("a.tenant_id = ?")
        params.append(f["tenant"])
    if f["from"]:
        where.append("a.ts >= ?")
        params.append(f"{f['from'].isoformat()}T00:00:00Z")
    if f["to"]:
        where.append("a.ts < ?")
        params.append(f"{(f['to'] + timedelta(days=1)).isoformat()}T00:00:00Z")
    return (" WHERE " + " AND ".join(where)) if where else "", params


def _scope() -> tuple[list[str], list]:
    """For someone who doesn't see every client: only what happened on the clients they see, and portal-wide
    rows that aren't by or about a client account of the others (whose name is one of that client's people:
    the user pages start their detail with it)."""
    if current().sees_all:
        return [], []
    unseen = sorted(unseen_client_accounts(g.db, g.user).values())
    return [
        "(a.tenant_id IS NULL OR a.tenant_id IN (SELECT value FROM json_each(?)))",
        "NOT EXISTS (SELECT 1 FROM json_each(?) h WHERE a.username = h.value OR a.detail = h.value"
        " OR a.detail LIKE h.value || ':%' OR a.detail LIKE h.value || ' %')",
    ], [json.dumps(sorted(_tenant_names())), json.dumps(unseen)]


def _tenant_names() -> dict[int, str]:
    # Only tenants this person may see are named (admins see all, archived included).
    return {t["id"]: t["name"] for t in visible_tenants(g.db, g.user, archived=True)}


def tenant_label(tenant_id: int | None, names: dict[int, str]) -> str:
    if tenant_id is None:
        return ""
    return names.get(tenant_id, f"Client #{tenant_id} (deleted or not visible)")


@bp.get("/admin/activity")
@needs("activity_log.read")
def index():
    f = _filters()
    page = max(1, request.args.get("page", 1, type=int))
    where, params = _where(f)
    rows = g.db.execute(
        f"SELECT a.* FROM audit_log a{where} ORDER BY a.id DESC LIMIT ? OFFSET ?",
        (*params, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE),
    ).fetchall()
    names = _tenant_names()
    actions = [r[0] for r in g.db.execute("SELECT DISTINCT action FROM audit_log ORDER BY action")]
    unseen = unseen_client_accounts(g.db, g.user)
    users = [
        r["username"]
        for r in g.db.execute("SELECT id, username FROM users ORDER BY username COLLATE NOCASE")
        if r["id"] not in unseen
    ]
    query = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in f.items() if v not in (None, "")}
    return render_template(
        "activity.html",
        rows=rows[:PAGE_SIZE],
        page=page,
        has_next=len(rows) > PAGE_SIZE,
        filters=f,
        query=query,
        names=names,
        tenant_label=tenant_label,
        actions=actions,
        users=users,
        tenants=sorted(names.items(), key=lambda kv: kv[1].lower()),
        noise=AUDIT_NOISE,
        noise_days=AUDIT_NOISE_DAYS,
    )


@bp.get("/admin/activity.csv")
@needs("activity_log.read")
def export():
    f = _filters()
    where, params = _where(f)
    rows = g.db.execute(
        f"SELECT a.* FROM audit_log a{where} ORDER BY a.id DESC LIMIT ?", (*params, CSV_LIMIT)
    ).fetchall()
    names = _tenant_names()
    table = Table(
        "Activity log",
        ["Time (UTC)", "User", "Action", "Client", "Address", "Detail"],
        [
            [
                r["ts"],
                r["username"] or "",
                r["action"],
                tenant_label(r["tenant_id"], names),
                r["ip"] or "",
                r["detail"] or "",
            ]
            for r in rows
        ],
    )
    audit(g.db, g.user["username"], "activity_log_downloaded", detail=f"{len(rows)} rows")
    return Response(
        to_csv(table),
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="office-sentry-activity.csv"'},
    )
