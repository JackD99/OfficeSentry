"""Settings → Activity log: the audit log for admins, with filters, paging and a CSV export."""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

from flask import Blueprint, Response, g, render_template, request

from .. import permissions
from ..db import AUDIT_NOISE, AUDIT_NOISE_DAYS, audit
from ..reports.evidence_answers import QUESTIONS as ANSWERED
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
        where.append("a.username = ? COLLATE NOCASE")  # as users.username is: "ADMIN" is admin
        params.append(f["user"])
    if f["action"]:
        where.append("a.action = ?")
        params.append(f["action"])
    if f["tenant"] is not None:
        where.append("a.tenant_id = ? AND a.ts >= COALESCE((SELECT created_at FROM tenants WHERE id = ?), '')")
        params.extend((f["tenant"], f["tenant"]))
    if f["from"]:
        where.append("a.ts >= ?")
        params.append(f"{f['from'].isoformat()}T00:00:00Z")
    if f["to"]:
        where.append("a.ts < ?")
        params.append(f"{(f['to'] + timedelta(days=1)).isoformat()}T00:00:00Z")
    return (" WHERE " + " AND ".join(where)) if where else "", params


def _scope() -> tuple[list[str], list]:
    """For someone who doesn't see every client: only what happened on the clients they see since each was
    added (before 2.4.0 a new client could get a deleted one's id, and with it the deleted one's rows), and
    portal-wide rows that aren't by or about a client account of the others (whose name is one of that
    client's people: the user pages start their detail with it)."""
    if current().sees_all:
        return [], []
    unseen = sorted(unseen_client_accounts(g.db, g.user).values())
    return [
        "(a.tenant_id IS NULL OR EXISTS (SELECT 1 FROM tenants t WHERE t.id = a.tenant_id AND a.ts >= t.created_at"
        " AND t.id IN (SELECT value FROM json_each(?))))",
        "NOT EXISTS (SELECT 1 FROM json_each(?) h WHERE a.username = h.value OR a.detail = h.value"
        " OR a.detail LIKE h.value || ':%' OR a.detail LIKE h.value || ' %')",
    ], [json.dumps(sorted(_tenant_names())), json.dumps(unseen)]


def _tenant_names() -> dict[int, str]:
    # Only tenants this person may see are named (admins see all, archived included).
    return {t["id"]: t["name"] for t in visible_tenants(g.db, g.user, archived=True)}


def _added() -> dict[int, str]:
    return {r["id"]: r["created_at"] for r in g.db.execute("SELECT id, created_at FROM tenants")}


def tenant_label(row, names: dict[int, str], added: dict[int, str]) -> str:
    """The row's client by name, unless the row is older than the client that has its id now."""
    tenant_id = row["tenant_id"]
    if tenant_id is None:
        return ""
    if tenant_id in names and row["ts"] >= added.get(tenant_id, ""):
        return names[tenant_id]
    return f"Client #{tenant_id} (deleted or not visible)"


QUESTIONS = {q.key: q.question for q in ANSWERED}
ACCESS_WORD = re.compile(r"([+-])([a-z_.]+)(?:@(\d+))?")


def detail_text(row, names: dict[int, str], added: dict[int, str]) -> str:
    """A row's detail in words where it was stored as keys: "jordan.desk: +collect@2" reads "jordan.desk:
    allowed Refresh data for Alpha Ltd" (a client named as the Client column names it), and an evidence answer
    names its question. Everything else shows the detail as stored. The CSV export keeps the stored detail and
    adds these words in its What happened column."""
    action, detail = row["action"], row["detail"] or ""
    if action == "user_access_changed" and ": " in detail:
        who, words = detail.split(": ", 1)
        if words == "none":
            return f"{who}: no exceptions to their role"
        parts = []
        for word in words.split(", "):
            m = ACCESS_WORD.fullmatch(word)
            if not m:
                return detail
            sign, key, tid = m.groups()
            cap = permissions.BY_KEY.get(key)
            client = tenant_label({"tenant_id": int(tid), "ts": row["ts"]}, names, added) if tid else ""
            what = (cap.title if cap else key) + (f" for {client}" if client else "")
            parts.append(f"{'allowed' if sign == '+' else 'not allowed'} {what}")
        return f"{who}: {'; '.join(parts)}"
    if action == "evidence_answer_set":
        m = re.fullmatch(r"([a-z0-9_]+): (.+) \(suggested (.+)\)", detail)
        if m and m[1] in QUESTIONS:
            return f"{QUESTIONS[m[1]]} Answer: {m[2]} (suggested: {m[3]})"
    if action == "evidence_answer_cleared" and detail in QUESTIONS:
        return f"{QUESTIONS[detail]} Back to the suggested answer"
    return detail


def action_text(action: str) -> str:
    """An action as the page names it: "tenant_updated" reads "Client updated"."""
    return action.replace("tenant", "client").replace("_", " ").capitalize()


def what_happened(row, names: dict[int, str], added: dict[int, str]) -> str:
    """A row in words, as the page reads it: the action, then its detail."""
    detail = detail_text(row, names, added)
    return f"{action_text(row['action'])}: {detail}" if detail else action_text(row["action"])


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
        added=_added(),
        detail_text=detail_text,
        action_text=action_text,
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
        f"SELECT a.* FROM audit_log a{where} ORDER BY a.id DESC LIMIT ?", (*params, CSV_LIMIT + 1)
    ).fetchall()
    cut, rows = len(rows) > CSV_LIMIT, rows[:CSV_LIMIT]
    names, added = _tenant_names(), _added()
    lines = [
        [
            r["ts"],
            r["username"] or "",
            r["action"],
            tenant_label(r, names, added),
            r["ip"] or "",
            r["detail"] or "",
            what_happened(r, names, added),
        ]
        for r in rows
    ]
    if cut:  # said in the file itself, so nobody takes it for the whole log
        lines.append(
            [f"Only the newest {CSV_LIMIT:,} rows. Choose dates to download the rest.", "", "", "", "", "", ""]
        )
    # The stored fields first, as they always were, so spreadsheets built on them keep working; then the row
    # as the page reads it.
    columns = ["Time (UTC)", "User", "Action", "Client", "Address", "Detail", "What happened"]
    table = Table("Activity log", columns, lines)
    audit(g.db, g.user["username"], "activity_log_downloaded", detail=f"{len(rows)} rows" + (", cut" if cut else ""))
    return Response(
        to_csv(table),
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="office-sentry-activity.csv"'},
    )
