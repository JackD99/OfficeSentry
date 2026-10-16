"""Findings lifecycle: turn each evaluation into durable history.

``sync()`` runs after collections finish. For every check that could run:
  * findings seen now are inserted, or have last_seen/severity refreshed
    (and are reopened if they had been resolved);
  * open findings no longer reported are marked resolved.
Checks that are "unavailable" leave their findings untouched, so missing data
never looks like a fix; so does a check for the objects it lists in ``keep``.
Several findings for one object are stored as one, at the worst severity.
It returns counts, plus ``opened``: the ids of findings new or reopened by
this sync (for alerts).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from .checks import SEVERITY_RANK, evaluate
from .db import transaction
from .snapshot import TenantSnapshot

ISO = "%Y-%m-%dT%H:%M:%SZ"


def finding_key(f) -> str:
    return f.object_id or f.subject


def by_key(findings) -> dict:
    """One finding per key. A check can report several things about one object (a domain's SPF ending in
    +all and needing too many lookups); the stored finding takes the worst severity and lists every detail,
    worst first, so a less serious problem never hides a worse one."""
    out: dict = {}
    for f in sorted(findings, key=lambda f: SEVERITY_RANK.get(f.severity, len(SEVERITY_RANK))):
        key = finding_key(f)
        first = out.get(key)
        if first is None:
            out[key] = f
        elif f.detail and f.detail not in first.detail.split("; "):
            out[key] = replace(first, detail=f"{first.detail}; {f.detail}" if first.detail else f.detail)
    return out


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
            current = by_key(r.findings)
            existing = {
                row["object_id"]: row
                for row in conn.execute(
                    "SELECT * FROM findings WHERE tenant_id = ? AND rule_key = ?", (tenant["id"], r.key)
                )
            }
            for key, f in current.items():
                row = existing.get(key)
                if row is None:
                    opened.append(
                        conn.execute(
                            "INSERT INTO findings (tenant_id, rule_key, object_id, severity, subject, detail,"
                            " first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (tenant["id"], r.key, key, f.severity, f.subject, f.detail, ts, ts),
                        ).lastrowid
                    )
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
                        # A problem that came back is a fresh one: nobody holds it and it isn't snoozed. The
                        # note stays, since why it wasn't fixed is still worth knowing.
                        _clear(conn, row["id"], hand=True)
                        opened.append(row["id"])
            for key, row in existing.items():
                if key not in current and key not in r.keep and row["resolved_at"] is None:
                    conn.execute("UPDATE findings SET resolved_at = ? WHERE id = ?", (ts, row["id"]))
                    # It keeps its holder, so what each person closed can be counted; a snooze is over.
                    _clear(conn, row["id"], hand=False)
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
            row["first_seen"],
            row["reopened_at"],
            accepted,
            row["accepted_by"],
            row["accepted_note"],
            row["accepted_until"],
            row["id"],
        )
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
    conn.execute(
        "UPDATE findings SET accepted_by = NULL, accepted_at = NULL, accepted_note = NULL,"
        " accepted_until = NULL WHERE id = ?",
        (finding_id,),
    )


# -- Acting on a finding (migration 0048) -------------------------------------------------------------
#
# What the team does with a finding, kept on its row in Office Sentry's own database and never written to a
# client's tenant: a staff note, who on the team holds it, and a snooze that keeps it out of Today for a
# while. None of it reaches a client's reports, PDFs, emails or pages.

NOTE_LENGTH = 2000


def _clear(conn, finding_id: int, hand: bool) -> None:
    columns = "snoozed_until = NULL, snoozed_by = NULL, snoozed_at = NULL"
    if hand:
        columns += ", handed_to = NULL, handed_by = NULL, handed_at = NULL"
    conn.execute(f"UPDATE findings SET {columns} WHERE id = ?", (finding_id,))


def note(conn, finding_id: int, text: str | None, by: str) -> None:
    """Keep a staff note on the finding, replacing the last one. Empty text removes it."""
    text = (text or "").strip()[:NOTE_LENGTH]
    if text:
        conn.execute(
            "UPDATE findings SET note = ?, note_by = ?, note_at = ? WHERE id = ?",
            (text, by, datetime.now(UTC).strftime(ISO), finding_id),
        )
    else:
        conn.execute("UPDATE findings SET note = NULL, note_by = NULL, note_at = NULL WHERE id = ?", (finding_id,))


def hand(conn, finding_id: int, to_user_id: int | None, by: str) -> None:
    """Hand the finding to a colleague (an enabled staff account), or to nobody. Whether that colleague may
    see the client is the caller's check (``web/finding.holders``): this only refuses what no caller could
    mean, a client account or a disabled or unknown one."""
    if to_user_id is None:
        _clear(conn, finding_id, hand=True)
        return
    row = conn.execute(
        "SELECT 1 FROM users WHERE id = ? AND role <> 'client' AND disabled = 0", (to_user_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"not an enabled staff account: {to_user_id}")
    conn.execute(
        "UPDATE findings SET handed_to = ?, handed_by = ?, handed_at = ? WHERE id = ?",
        (to_user_id, by, datetime.now(UTC).strftime(ISO), finding_id),
    )


def snooze(conn, finding_id: int, until: str | None, by: str) -> None:
    """Keep the finding out of Today until ``until`` (an ISO timestamp in UTC), or end a snooze (None)."""
    if until is None:
        conn.execute(
            "UPDATE findings SET snoozed_until = NULL, snoozed_by = NULL, snoozed_at = NULL WHERE id = ?",
            (finding_id,),
        )
        return
    conn.execute(
        "UPDATE findings SET snoozed_until = ?, snoozed_by = ?, snoozed_at = ? WHERE id = ?",
        (until, by, datetime.now(UTC).strftime(ISO), finding_id),
    )


def is_snoozed(row, now: datetime | None = None) -> bool:
    until = row["snoozed_until"]
    return bool(until) and until > (now or datetime.now(UTC)).strftime(ISO)
