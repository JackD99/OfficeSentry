"""The report archive: a copy of every report file a client was actually sent.

Reports are built live from snapshots, and snapshots are pruned, so a PDF
rebuilt today can't show what went out in March. ``delivery.send_schedule``
calls ``archive()`` after a report email is sent, and every attachment is kept
in ``issued_reports`` with its period, recipients and SHA-256. Rows are never
pruned; deleting the tenant deletes them (``ON DELETE CASCADE``), and backups
copy them with the database.
"""

from __future__ import annotations

import calendar
import hashlib
import re
from dataclasses import dataclass

from .db import utcnow

# Columns for listing: everything but the bytes.
COLUMNS = ("id, tenant_id, schedule_id, kind, title, period, version, issued_at, issued_by, trigger,"
           " recipients, bcc, filename, content_type, sha256, size")


@dataclass(frozen=True)
class IssuedFile:
    kind: str           # report key ("review", "evidence", "data:<key>")
    title: str
    filename: str
    content_type: str
    data: bytes


def archive(conn, tenant_id: int, files: list[IssuedFile], *, period: str | None, trigger: str,
            user: str | None, to=(), bcc=(), schedule_id: int | None = None,
            now: str | None = None) -> list[int]:
    """Store each file sent to ``tenant_id``. A file sent again for the same period gets the next version."""
    issued_at = now or utcnow()
    ids = []
    for f in files:
        version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM issued_reports WHERE tenant_id = ? AND filename = ?"
            " AND period IS ?", (tenant_id, f.filename, period)).fetchone()[0]
        ids.append(conn.execute(
            "INSERT INTO issued_reports (tenant_id, schedule_id, kind, title, period, version, issued_at,"
            " issued_by, trigger, recipients, bcc, filename, content_type, sha256, size, data)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, schedule_id, f.kind, f.title, period, version, issued_at, user, trigger,
             ", ".join(to), ", ".join(bcc), f.filename, f.content_type, hashlib.sha256(f.data).hexdigest(),
             len(f.data), f.data)).lastrowid)
    return ids


def period_label(period: str | None) -> str:
    """ "2026-10" -> "October 2026", "2026-W41" -> "Week 41, 2026", "2026-Q4" -> "Q4 2026"."""
    m = re.fullmatch(r"(\d{4})-(?:(\d{2})|W(\d{2})|Q([1-4]))", period or "")
    if not m:
        return period or ""
    year, month, week, quarter = m.groups()
    if month:
        return f"{calendar.month_name[int(month)]} {year}"
    if week:
        return f"Week {int(week)}, {year}"
    return f"Q{quarter} {year}"


def size_label(size: int) -> str:
    return f"{size / 1_000_000:.1f} MB" if size >= 1_000_000 else f"{max(1, round(size / 1000))} KB"


def history(conn, tenant_id: int) -> list[dict]:
    """One entry per send (an email's files together, PDF first), newest first."""
    groups: dict[tuple, dict] = {}
    for r in conn.execute(f"SELECT {COLUMNS} FROM issued_reports WHERE tenant_id = ?"
                          " ORDER BY issued_at DESC, id", (tenant_id,)):
        key = (r["issued_at"], r["schedule_id"], r["trigger"])
        entry = groups.setdefault(key, {"issued_at": r["issued_at"], "issued_by": r["issued_by"],
                                        "trigger": r["trigger"], "period": r["period"],
                                        "period_label": period_label(r["period"]),
                                        "recipients": r["recipients"], "bcc": r["bcc"], "files": []})
        entry["files"].append(dict(r, format=r["filename"].rsplit(".", 1)[-1].upper(),
                                   size_label=size_label(r["size"])))
    for entry in groups.values():
        entry["files"].sort(key=lambda f: (not f["filename"].endswith(".pdf"), f["id"]))
        entry["titles"] = list(dict.fromkeys(f["title"] for f in entry["files"]))
        entry["version"] = max(f["version"] for f in entry["files"])
    return list(groups.values())


def get(conn, tenant_id: int, report_id: int):
    """One stored file, with its bytes, only if it belongs to ``tenant_id``."""
    return conn.execute("SELECT * FROM issued_reports WHERE id = ? AND tenant_id = ?",
                        (report_id, tenant_id)).fetchone()
