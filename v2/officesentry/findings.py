"""Findings lifecycle: turn each evaluation into durable history.

``sync()`` runs after collections finish. For every check that could run:
  * findings seen now are inserted, or have last_seen/severity refreshed
    (and are reopened if they had been resolved);
  * open findings no longer reported are marked resolved.
Checks that are "unavailable" leave their findings untouched, so missing data
never looks like a fix.
It returns counts, plus ``opened``: the ids of findings new or reopened by
this sync (for alerts).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .checks import evaluate
from .db import transaction
from .snapshot import TenantSnapshot

ISO = "%Y-%m-%dT%H:%M:%SZ"


def finding_key(f) -> str:
    return f.object_id or f.subject


def sync(conn, tenant, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    ts = now.strftime(ISO)
    results = evaluate(TenantSnapshot(conn, tenant), now)
    stats = {"new": 0, "reopened": 0, "resolved": 0}
    opened: list[int] = []
    with transaction(conn, immediate=True):
        for r in results:
            if r.status == "unavailable":
                continue
            current = {finding_key(f): f for f in r.findings}
            existing = {row["object_id"]: row for row in conn.execute(
                "SELECT * FROM findings WHERE tenant_id = ? AND rule_key = ?", (tenant["id"], r.key))}
            for key, f in current.items():
                row = existing.get(key)
                if row is None:
                    opened.append(conn.execute(
                        "INSERT INTO findings (tenant_id, rule_key, object_id, severity, subject, detail,"
                        " first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (tenant["id"], r.key, key, f.severity, f.subject, f.detail, ts, ts),
                    ).lastrowid)
                    stats["new"] += 1
                else:
                    reopened = row["resolved_at"] is not None
                    conn.execute(
                        "UPDATE findings SET severity = ?, subject = ?, detail = ?, last_seen = ?,"
                        " resolved_at = NULL, reopened_at = CASE WHEN ? THEN ? ELSE reopened_at END WHERE id = ?",
                        (f.severity, f.subject, f.detail, ts, int(reopened), ts, row["id"]),
                    )
                    stats["reopened"] += int(reopened)
                    if reopened:
                        opened.append(row["id"])
            for key, row in existing.items():
                if key not in current and row["resolved_at"] is None:
                    conn.execute("UPDATE findings SET resolved_at = ? WHERE id = ?", (ts, row["id"]))
                    stats["resolved"] += 1
    return {**stats, "opened": opened}


@dataclass
class Lifecycle:
    first_seen: str
    reopened_at: str | None
    accepted: bool
    accepted_by: str | None
    accepted_note: str | None
    accepted_until: str | None
    finding_id: int

    def is_new_since(self, since: str) -> bool:
        return self.first_seen >= since or (self.reopened_at or "") >= since


def lifecycle_map(conn, tenant_id: int, now: datetime | None = None) -> dict[tuple[str, str], Lifecycle]:
    ts = (now or datetime.now(UTC)).strftime(ISO)
    out = {}
    for row in conn.execute("SELECT * FROM findings WHERE tenant_id = ? AND resolved_at IS NULL", (tenant_id,)):
        accepted = bool(row["accepted_at"]) and (row["accepted_until"] is None or row["accepted_until"] > ts)
        out[(row["rule_key"], row["object_id"])] = Lifecycle(
            row["first_seen"], row["reopened_at"], accepted, row["accepted_by"], row["accepted_note"],
            row["accepted_until"], row["id"])
    return out


def resolved_since(conn, tenant_id: int, since: str) -> list:
    return conn.execute(
        "SELECT * FROM findings WHERE tenant_id = ? AND resolved_at >= ? ORDER BY resolved_at DESC",
        (tenant_id, since),
    ).fetchall()


def accept(conn, finding_id: int, username: str, note: str, until: str | None) -> None:
    conn.execute(
        "UPDATE findings SET accepted_by = ?, accepted_at = ?, accepted_note = ?, accepted_until = ? WHERE id = ?",
        (username, datetime.now(UTC).strftime(ISO), note, until, finding_id),
    )


def unaccept(conn, finding_id: int) -> None:
    conn.execute("UPDATE findings SET accepted_by = NULL, accepted_at = NULL, accepted_note = NULL,"
                 " accepted_until = NULL WHERE id = ?", (finding_id,))
