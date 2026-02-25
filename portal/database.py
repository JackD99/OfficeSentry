"""
portal/database.py — SQLite persistence layer for the M365 Audit Portal.
Manages tenants, jobs, audit trail, and operator accounts.
"""

import json
import shutil
import sqlite3
import uuid
import hashlib
import base64
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).parent.parent / "data" / "portal.db"
BACKUP_DIR = Path(__file__).parent.parent / "data" / "backups"


# ---------------------------------------------------------------------------
# Encryption helpers (PFX password at rest)
# ---------------------------------------------------------------------------

def _derive_key(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()


def encrypt_value(plaintext: str, secret: str) -> str:
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(_derive_key(secret))
    return Fernet(key).encrypt(plaintext.encode()).decode()


def decrypt_value(token: str, secret: str) -> str:
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(_derive_key(secret))
    return Fernet(key).decrypt(token.encode()).decode()


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

@contextmanager
def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT    UNIQUE NOT NULL,
    password_hash TEXT  NOT NULL,
    display_name TEXT,
    is_admin    INTEGER DEFAULT 0,
    created_at  TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tenants (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    domain           TEXT NOT NULL,
    tenant_id        TEXT DEFAULT '',
    client_id        TEXT DEFAULT '',
    app_display_name TEXT DEFAULT 'OfficeSentry',
    cert_subject     TEXT DEFAULT 'CN=M365 Audit',
    cert_years_valid INTEGER DEFAULT 2,
    pfx_password_enc TEXT DEFAULT '',
    notes            TEXT DEFAULT '',
    created_at       TEXT DEFAULT (datetime('now')),
    updated_at       TEXT DEFAULT (datetime('now')),
    last_audit_at    TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT,
    type        TEXT NOT NULL,
    label       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',
    operator    TEXT DEFAULT '',
    started_at  TEXT DEFAULT (datetime('now')),
    ended_at    TEXT,
    output      TEXT DEFAULT '[]',
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT DEFAULT (datetime('now')),
    tenant_id   TEXT,
    tenant_name TEXT,
    operator    TEXT NOT NULL,
    action      TEXT NOT NULL,
    details     TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS saved_views (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    report_type TEXT NOT NULL DEFAULT '',
    columns     TEXT DEFAULT '[]',
    filters     TEXT DEFAULT '{}',
    sort_col    TEXT DEFAULT '',
    sort_dir    TEXT DEFAULT 'asc',
    chart_type  TEXT DEFAULT '',
    chart_col   TEXT DEFAULT '',
    created_by  TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS report_schedules (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    tenant_id   TEXT,
    audit_type  TEXT NOT NULL,
    audit_params TEXT DEFAULT '{}',
    frequency   TEXT NOT NULL DEFAULT 'weekly',
    day_of_week INTEGER DEFAULT 1,
    hour        INTEGER DEFAULT 9,
    enabled     INTEGER DEFAULT 1,
    last_run    TEXT,
    next_run    TEXT,
    created_by  TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tenant_groups (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    color       TEXT DEFAULT 'primary',
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tenant_group_members (
    group_id    TEXT NOT NULL,
    tenant_id   TEXT NOT NULL,
    PRIMARY KEY (group_id, tenant_id),
    FOREIGN KEY (group_id) REFERENCES tenant_groups(id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS standards_templates (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    checks      TEXT DEFAULT '[]',
    created_by  TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tenant_standards (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    template_id TEXT NOT NULL,
    action      TEXT NOT NULL DEFAULT 'report',
    applied_by  TEXT NOT NULL,
    applied_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    FOREIGN KEY (template_id) REFERENCES standards_templates(id) ON DELETE CASCADE,
    UNIQUE (tenant_id, template_id)
);

CREATE TABLE IF NOT EXISTS bpa_results (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    template_id TEXT,
    check_key   TEXT NOT NULL,
    check_name  TEXT NOT NULL,
    category    TEXT DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'unknown',
    detail      TEXT DEFAULT '',
    expected    TEXT DEFAULT '',
    actual      TEXT DEFAULT '',
    impact      TEXT DEFAULT 'Medium',
    scanned_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS alert_rules (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    alert_type  TEXT NOT NULL DEFAULT 'audit_log',
    conditions  TEXT DEFAULT '{}',
    actions     TEXT DEFAULT '[]',
    tenant_id   TEXT,
    enabled     INTEGER DEFAULT 1,
    created_by  TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS alert_history (
    id          TEXT PRIMARY KEY,
    rule_id     TEXT NOT NULL,
    tenant_id   TEXT,
    tenant_name TEXT DEFAULT '',
    title       TEXT NOT NULL,
    detail      TEXT DEFAULT '',
    severity    TEXT DEFAULT 'info',
    status      TEXT DEFAULT 'new',
    delivered_via TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now')),
    resolved_at TEXT,
    FOREIGN KEY (rule_id) REFERENCES alert_rules(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS secure_scores (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    current_score REAL DEFAULT 0,
    max_score   REAL DEFAULT 0,
    average_comparative_score REAL DEFAULT 0,
    fetched_at  TEXT DEFAULT (datetime('now')),
    details     TEXT DEFAULT '{}',
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);
"""


SCHEMA_VERSION = 3

_MIGRATIONS = {
    2: """
CREATE TABLE IF NOT EXISTS saved_views (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    report_type TEXT NOT NULL DEFAULT '',
    columns     TEXT DEFAULT '[]',
    filters     TEXT DEFAULT '{}',
    sort_col    TEXT DEFAULT '',
    sort_dir    TEXT DEFAULT 'asc',
    chart_type  TEXT DEFAULT '',
    chart_col   TEXT DEFAULT '',
    created_by  TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS report_schedules (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    tenant_id   TEXT,
    audit_type  TEXT NOT NULL,
    audit_params TEXT DEFAULT '{}',
    frequency   TEXT NOT NULL DEFAULT 'weekly',
    day_of_week INTEGER DEFAULT 1,
    hour        INTEGER DEFAULT 9,
    enabled     INTEGER DEFAULT 1,
    last_run    TEXT,
    next_run    TEXT,
    created_by  TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);
""",
    3: """
CREATE TABLE IF NOT EXISTS tenant_groups (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    color       TEXT DEFAULT 'primary',
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tenant_group_members (
    group_id    TEXT NOT NULL,
    tenant_id   TEXT NOT NULL,
    PRIMARY KEY (group_id, tenant_id),
    FOREIGN KEY (group_id) REFERENCES tenant_groups(id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS standards_templates (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    checks      TEXT DEFAULT '[]',
    created_by  TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tenant_standards (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    template_id TEXT NOT NULL,
    action      TEXT NOT NULL DEFAULT 'report',
    applied_by  TEXT NOT NULL,
    applied_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    FOREIGN KEY (template_id) REFERENCES standards_templates(id) ON DELETE CASCADE,
    UNIQUE (tenant_id, template_id)
);

CREATE TABLE IF NOT EXISTS bpa_results (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    template_id TEXT,
    check_key   TEXT NOT NULL,
    check_name  TEXT NOT NULL,
    category    TEXT DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'unknown',
    detail      TEXT DEFAULT '',
    expected    TEXT DEFAULT '',
    actual      TEXT DEFAULT '',
    impact      TEXT DEFAULT 'Medium',
    scanned_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS alert_rules (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    alert_type  TEXT NOT NULL DEFAULT 'audit_log',
    conditions  TEXT DEFAULT '{}',
    actions     TEXT DEFAULT '[]',
    tenant_id   TEXT,
    enabled     INTEGER DEFAULT 1,
    created_by  TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS alert_history (
    id          TEXT PRIMARY KEY,
    rule_id     TEXT NOT NULL,
    tenant_id   TEXT,
    tenant_name TEXT DEFAULT '',
    title       TEXT NOT NULL,
    detail      TEXT DEFAULT '',
    severity    TEXT DEFAULT 'info',
    status      TEXT DEFAULT 'new',
    delivered_via TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now')),
    resolved_at TEXT,
    FOREIGN KEY (rule_id) REFERENCES alert_rules(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS secure_scores (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    current_score REAL DEFAULT 0,
    max_score   REAL DEFAULT 0,
    average_comparative_score REAL DEFAULT 0,
    fetched_at  TEXT DEFAULT (datetime('now')),
    details     TEXT DEFAULT '{}',
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);
""",
}


def _get_schema_version(conn) -> int:
    try:
        row = conn.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        return row["version"] if row else 0
    except sqlite3.OperationalError:
        return 0


def _run_migrations(conn):
    current = _get_schema_version(conn)
    for ver in sorted(_MIGRATIONS):
        if ver > current:
            conn.executescript(_MIGRATIONS[ver])
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (ver,)
            )


def init_db():
    with get_db() as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "  version INTEGER PRIMARY KEY,"
            "  applied_at TEXT DEFAULT (datetime('now'))"
            ")"
        )
        if _get_schema_version(conn) == 0:
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,)
            )
        _run_migrations(conn)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    from werkzeug.security import generate_password_hash
    return generate_password_hash(password)


def check_password(stored_hash: str, password: str) -> bool:
    from werkzeug.security import check_password_hash
    return check_password_hash(stored_hash, password)


def create_user(username: str, password: str, display_name: str = "",
                is_admin: int = 0) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, display_name, is_admin) "
            "VALUES (?, ?, ?, ?)",
            (username, hash_password(password), display_name or username, is_admin)
        )
        return cur.lastrowid


def get_user(username: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        return dict(row) if row else None


def user_count() -> int:
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def update_password(username: str, new_password: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (hash_password(new_password), username)
        )


def list_users() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, username, display_name, is_admin, created_at "
            "FROM users ORDER BY username"
        ).fetchall()
        return [dict(r) for r in rows]


def delete_user(username: str) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM users WHERE username = ?", (username,)
        )
        return cur.rowcount > 0


def update_user(username: str, **fields):
    if not fields:
        return
    allowed = {"display_name", "is_admin"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [username]
    with get_db() as conn:
        conn.execute(
            f"UPDATE users SET {set_clause} WHERE username = ?", values
        )


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------

def create_tenant(name: str, domain: str, tenant_id: str = "",
                  notes: str = "") -> str:
    tid = str(uuid.uuid4())[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO tenants (id, name, domain, tenant_id, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (tid, name, domain, tenant_id, notes)
        )
    return tid


def get_tenant(tenant_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        return dict(row) if row else None


def list_tenants() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tenants ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]


def update_tenant(row_id: str, **fields):
    if not fields:
        return
    fields["updated_at"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [row_id]
    with get_db() as conn:
        conn.execute(
            f"UPDATE tenants SET {set_clause} WHERE id = ?", values
        )


def delete_tenant(tenant_id: str):
    with get_db() as conn:
        conn.execute("DELETE FROM jobs WHERE tenant_id = ?", (tenant_id,))
        conn.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))


def touch_tenant_audit(tenant_id: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE tenants SET last_audit_at = datetime('now') WHERE id = ?",
            (tenant_id,)
        )


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def create_job(label: str, job_type: str, tenant_id: str = None,
               operator: str = "") -> str:
    jid = str(uuid.uuid4())[:8]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO jobs (id, tenant_id, type, label, operator, output) "
            "VALUES (?, ?, ?, ?, ?, '[]')",
            (jid, tenant_id, job_type, label, operator)
        )
    return jid


def get_job(job_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["output"] = json.loads(d["output"]) if d["output"] else []
        return d


def append_job_output(job_id: str, line: str):
    with get_db() as conn:
        row = conn.execute("SELECT output FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return
        lines = json.loads(row["output"]) if row["output"] else []
        lines.append(line)
        conn.execute(
            "UPDATE jobs SET output = ? WHERE id = ?",
            (json.dumps(lines), job_id)
        )


def finish_job(job_id: str, status: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, ended_at = datetime('now') WHERE id = ?",
            (status, job_id)
        )


def list_jobs(tenant_id: str = None, limit: int = 50) -> list[dict]:
    with get_db() as conn:
        if tenant_id:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE tenant_id = ? ORDER BY started_at DESC LIMIT ?",
                (tenant_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["output"] = json.loads(d["output"]) if d["output"] else []
            result.append(d)
        return result


def list_running_jobs() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = 'running' ORDER BY started_at DESC"
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["output"] = json.loads(d["output"]) if d["output"] else []
            result.append(d)
        return result


# ---------------------------------------------------------------------------
# Audit Log
# ---------------------------------------------------------------------------

def log_action(operator: str, action: str, tenant_id: str = None,
               tenant_name: str = "", details: str = ""):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO audit_log (tenant_id, tenant_name, operator, action, details) "
            "VALUES (?, ?, ?, ?, ?)",
            (tenant_id, tenant_name, operator, action, details)
        )


def get_audit_log(limit: int = 200, tenant_id: str = None) -> list[dict]:
    with get_db() as conn:
        if tenant_id:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE tenant_id = ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (tenant_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Dashboard statistics
# ---------------------------------------------------------------------------

def get_dashboard_stats() -> dict:
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
        configured = conn.execute(
            "SELECT COUNT(*) FROM tenants WHERE client_id != ''"
        ).fetchone()[0]
        audited = conn.execute(
            "SELECT COUNT(*) FROM tenants WHERE last_audit_at IS NOT NULL"
        ).fetchone()[0]
        running = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'running'"
        ).fetchone()[0]
        total_jobs = conn.execute(
            "SELECT COUNT(*) FROM jobs"
        ).fetchone()[0]
        completed_jobs = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'done'"
        ).fetchone()[0]
        failed_jobs = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'failed'"
        ).fetchone()[0]
        return {
            "total_tenants": total,
            "configured_tenants": configured,
            "audited_tenants": audited,
            "running_jobs": running,
            "total_jobs": total_jobs,
            "completed_jobs": completed_jobs,
            "failed_jobs": failed_jobs,
        }


# ---------------------------------------------------------------------------
# Database maintenance
# ---------------------------------------------------------------------------

def backup_db(max_backups: int = 5) -> str | None:
    """Create a timestamped backup of the database. Returns backup path or None on failure."""
    if not DB_PATH.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = BACKUP_DIR / f"portal_{stamp}.db"
    try:
        shutil.copy2(str(DB_PATH), str(backup_path))
    except OSError:
        return None
    # Prune old backups
    backups = sorted(BACKUP_DIR.glob("portal_*.db"), key=lambda p: p.stat().st_mtime)
    while len(backups) > max_backups:
        backups.pop(0).unlink(missing_ok=True)
    return str(backup_path)


def check_integrity() -> dict:
    """Run SQLite integrity check and return results."""
    with get_db() as conn:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        ok = result[0] == "ok" if result else False
        version = _get_schema_version(conn)
        user_ct = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        tenant_ct = conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
        job_ct = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        return {
            "integrity": "ok" if ok else result[0],
            "schema_version": version,
            "users": user_ct,
            "tenants": tenant_ct,
            "jobs": job_ct,
        }


# ---------------------------------------------------------------------------
# Saved Views
# ---------------------------------------------------------------------------

def create_saved_view(name: str, report_type: str, columns: list, filters: dict,
                      sort_col: str = "", sort_dir: str = "asc",
                      chart_type: str = "", chart_col: str = "",
                      created_by: str = "") -> str:
    vid = str(uuid.uuid4())[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO saved_views (id, name, report_type, columns, filters, "
            "sort_col, sort_dir, chart_type, chart_col, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (vid, name, report_type, json.dumps(columns), json.dumps(filters),
             sort_col, sort_dir, chart_type, chart_col, created_by)
        )
    return vid


def get_saved_view(view_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM saved_views WHERE id = ?", (view_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["columns"] = json.loads(d["columns"]) if d["columns"] else []
        d["filters"] = json.loads(d["filters"]) if d["filters"] else {}
        return d


def list_saved_views(created_by: str = None) -> list[dict]:
    with get_db() as conn:
        if created_by:
            rows = conn.execute(
                "SELECT * FROM saved_views WHERE created_by = ? ORDER BY name",
                (created_by,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM saved_views ORDER BY name").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["columns"] = json.loads(d["columns"]) if d["columns"] else []
            d["filters"] = json.loads(d["filters"]) if d["filters"] else {}
            result.append(d)
        return result


def delete_saved_view(view_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM saved_views WHERE id = ?", (view_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Report Schedules
# ---------------------------------------------------------------------------

def create_schedule(name: str, tenant_id: str, audit_type: str,
                    audit_params: dict, frequency: str = "weekly",
                    day_of_week: int = 1, hour: int = 9,
                    created_by: str = "") -> str:
    sid = str(uuid.uuid4())[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO report_schedules (id, name, tenant_id, audit_type, "
            "audit_params, frequency, day_of_week, hour, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, name, tenant_id, audit_type, json.dumps(audit_params),
             frequency, day_of_week, hour, created_by)
        )
    return sid


def list_schedules(tenant_id: str = None) -> list[dict]:
    with get_db() as conn:
        if tenant_id:
            rows = conn.execute(
                "SELECT * FROM report_schedules WHERE tenant_id = ? ORDER BY name",
                (tenant_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM report_schedules ORDER BY name"
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["audit_params"] = json.loads(d["audit_params"]) if d["audit_params"] else {}
            result.append(d)
        return result


def get_schedule(schedule_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM report_schedules WHERE id = ?", (schedule_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["audit_params"] = json.loads(d["audit_params"]) if d["audit_params"] else {}
        return d


def update_schedule(schedule_id: str, **fields):
    if not fields:
        return
    allowed = {"name", "audit_type", "audit_params", "frequency",
               "day_of_week", "hour", "enabled", "last_run", "next_run"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if "audit_params" in fields and isinstance(fields["audit_params"], dict):
        fields["audit_params"] = json.dumps(fields["audit_params"])
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [schedule_id]
    with get_db() as conn:
        conn.execute(
            f"UPDATE report_schedules SET {set_clause} WHERE id = ?", values
        )


def delete_schedule(schedule_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM report_schedules WHERE id = ?", (schedule_id,)
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Enhanced Dashboard Statistics
# ---------------------------------------------------------------------------

def get_job_trends(days: int = 30) -> list[dict]:
    """Get daily job counts for the last N days."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT date(started_at) as day, "
            "COUNT(*) as total, "
            "SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) as completed, "
            "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed "
            "FROM jobs WHERE started_at >= date('now', ?) "
            "GROUP BY date(started_at) ORDER BY day",
            (f"-{days} days",)
        ).fetchall()
        return [dict(r) for r in rows]


def get_job_type_distribution() -> list[dict]:
    """Get job counts grouped by type."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT type, COUNT(*) as count FROM jobs GROUP BY type ORDER BY count DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_report_type_counts(tenant_dirs: list) -> dict:
    """Count report files by type from filesystem paths (called from route)."""
    counts = {}
    for out_dir in tenant_dirs:
        if not out_dir.exists():
            continue
        for f in out_dir.rglob("*"):
            if not f.is_file():
                continue
            n = f.name.lower()
            if "securityscan" in n or "365inspect" in n:
                rt = "Security Scan"
            elif "userperms" in n:
                rt = "User Permissions"
            elif "groups" in n:
                rt = "Groups"
            elif "siteperm" in n:
                rt = "Site Permissions"
            elif "external" in n:
                rt = "External Sharing"
            elif "ual" in n or "auditlog" in n:
                rt = "Audit Log"
            elif "inboxrule" in n:
                rt = "Inbox Rules"
            elif "signin" in n:
                rt = "Sign-In Logs"
            elif "oauth" in n:
                rt = "OAuth Consent"
            elif "msgtrace" in n or "messagetrace" in n:
                rt = "Message Trace"
            else:
                rt = "Other"
            counts[rt] = counts.get(rt, 0) + 1
    return counts


def purge_old_jobs(days: int = 90) -> int:
    """Delete completed/failed jobs older than the specified number of days. Returns count deleted."""
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM jobs WHERE status IN ('done', 'failed', 'cancelled') "
            "AND started_at < datetime('now', ?)",
            (f"-{days} days",)
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Tenant Groups
# ---------------------------------------------------------------------------

def create_tenant_group(name: str, description: str = "",
                        color: str = "primary") -> str:
    gid = str(uuid.uuid4())[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO tenant_groups (id, name, description, color) "
            "VALUES (?, ?, ?, ?)",
            (gid, name, description, color)
        )
    return gid


def get_tenant_group(group_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM tenant_groups WHERE id = ?", (group_id,)
        ).fetchone()
        return dict(row) if row else None


def list_tenant_groups() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT g.*, COUNT(m.tenant_id) as member_count "
            "FROM tenant_groups g "
            "LEFT JOIN tenant_group_members m ON g.id = m.group_id "
            "GROUP BY g.id ORDER BY g.name"
        ).fetchall()
        return [dict(r) for r in rows]


def update_tenant_group(group_id: str, **fields):
    if not fields:
        return
    allowed = {"name", "description", "color"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [group_id]
    with get_db() as conn:
        conn.execute(
            f"UPDATE tenant_groups SET {set_clause} WHERE id = ?", values
        )


def delete_tenant_group(group_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM tenant_groups WHERE id = ?", (group_id,)
        )
        return cur.rowcount > 0


def add_tenant_to_group(group_id: str, tenant_id: str):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO tenant_group_members (group_id, tenant_id) "
            "VALUES (?, ?)",
            (group_id, tenant_id)
        )


def remove_tenant_from_group(group_id: str, tenant_id: str):
    with get_db() as conn:
        conn.execute(
            "DELETE FROM tenant_group_members WHERE group_id = ? AND tenant_id = ?",
            (group_id, tenant_id)
        )


def get_group_tenants(group_id: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT t.* FROM tenants t "
            "JOIN tenant_group_members m ON t.id = m.tenant_id "
            "WHERE m.group_id = ? ORDER BY t.name",
            (group_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_tenant_groups_for_tenant(tenant_id: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT g.* FROM tenant_groups g "
            "JOIN tenant_group_members m ON g.id = m.group_id "
            "WHERE m.tenant_id = ? ORDER BY g.name",
            (tenant_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def set_group_members(group_id: str, tenant_ids: list[str]):
    """Replace all members of a group."""
    with get_db() as conn:
        conn.execute(
            "DELETE FROM tenant_group_members WHERE group_id = ?", (group_id,)
        )
        for tid in tenant_ids:
            conn.execute(
                "INSERT INTO tenant_group_members (group_id, tenant_id) "
                "VALUES (?, ?)",
                (group_id, tid)
            )


# ---------------------------------------------------------------------------
# Standards Templates
# ---------------------------------------------------------------------------

def create_standards_template(name: str, description: str = "",
                              checks: list = None,
                              created_by: str = "") -> str:
    tid = str(uuid.uuid4())[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO standards_templates (id, name, description, checks, created_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (tid, name, description, json.dumps(checks or []), created_by)
        )
    return tid


def get_standards_template(template_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM standards_templates WHERE id = ?", (template_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["checks"] = json.loads(d["checks"]) if d["checks"] else []
        return d


def list_standards_templates() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM standards_templates ORDER BY name"
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["checks"] = json.loads(d["checks"]) if d["checks"] else []
            result.append(d)
        return result


def update_standards_template(template_id: str, **fields):
    if not fields:
        return
    allowed = {"name", "description", "checks"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if "checks" in fields and isinstance(fields["checks"], list):
        fields["checks"] = json.dumps(fields["checks"])
    fields["updated_at"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [template_id]
    with get_db() as conn:
        conn.execute(
            f"UPDATE standards_templates SET {set_clause} WHERE id = ?", values
        )


def delete_standards_template(template_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM standards_templates WHERE id = ?", (template_id,)
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Tenant Standards Assignments
# ---------------------------------------------------------------------------

def assign_standard_to_tenant(tenant_id: str, template_id: str,
                              action: str = "report",
                              applied_by: str = "") -> str:
    aid = str(uuid.uuid4())[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO tenant_standards "
            "(id, tenant_id, template_id, action, applied_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (aid, tenant_id, template_id, action, applied_by)
        )
    return aid


def get_tenant_standards(tenant_id: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT ts.*, st.name as template_name, st.description as template_desc "
            "FROM tenant_standards ts "
            "JOIN standards_templates st ON ts.template_id = st.id "
            "WHERE ts.tenant_id = ? ORDER BY st.name",
            (tenant_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def remove_tenant_standard(tenant_id: str, template_id: str):
    with get_db() as conn:
        conn.execute(
            "DELETE FROM tenant_standards WHERE tenant_id = ? AND template_id = ?",
            (tenant_id, template_id)
        )


# ---------------------------------------------------------------------------
# BPA Results
# ---------------------------------------------------------------------------

def save_bpa_result(tenant_id: str, check_key: str, check_name: str,
                    status: str, category: str = "", detail: str = "",
                    expected: str = "", actual: str = "",
                    impact: str = "Medium", template_id: str = None) -> str:
    rid = str(uuid.uuid4())[:12]
    with get_db() as conn:
        # Remove old result for same check on same tenant
        conn.execute(
            "DELETE FROM bpa_results WHERE tenant_id = ? AND check_key = ?",
            (tenant_id, check_key)
        )
        conn.execute(
            "INSERT INTO bpa_results (id, tenant_id, template_id, check_key, "
            "check_name, category, status, detail, expected, actual, impact) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, tenant_id, template_id, check_key, check_name, category,
             status, detail, expected, actual, impact)
        )
    return rid


def get_bpa_results(tenant_id: str = None) -> list[dict]:
    with get_db() as conn:
        if tenant_id:
            rows = conn.execute(
                "SELECT b.*, t.name as tenant_name, t.domain as tenant_domain "
                "FROM bpa_results b "
                "JOIN tenants t ON b.tenant_id = t.id "
                "WHERE b.tenant_id = ? ORDER BY b.category, b.check_name",
                (tenant_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT b.*, t.name as tenant_name, t.domain as tenant_domain "
                "FROM bpa_results b "
                "JOIN tenants t ON b.tenant_id = t.id "
                "ORDER BY t.name, b.category, b.check_name"
            ).fetchall()
        return [dict(r) for r in rows]


def get_bpa_summary() -> dict:
    """Get BPA pass/fail/warn counts grouped by tenant."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT b.tenant_id, t.name as tenant_name, t.domain, "
            "SUM(CASE WHEN b.status = 'pass' THEN 1 ELSE 0 END) as pass_count, "
            "SUM(CASE WHEN b.status = 'fail' THEN 1 ELSE 0 END) as fail_count, "
            "SUM(CASE WHEN b.status = 'warn' THEN 1 ELSE 0 END) as warn_count, "
            "COUNT(*) as total_checks, "
            "MAX(b.scanned_at) as last_scan "
            "FROM bpa_results b "
            "JOIN tenants t ON b.tenant_id = t.id "
            "GROUP BY b.tenant_id ORDER BY t.name"
        ).fetchall()
        return [dict(r) for r in rows]


def clear_bpa_results(tenant_id: str):
    """Remove all BPA results for a tenant (before re-scan)."""
    with get_db() as conn:
        conn.execute(
            "DELETE FROM bpa_results WHERE tenant_id = ?", (tenant_id,)
        )


# ---------------------------------------------------------------------------
# Alert Rules
# ---------------------------------------------------------------------------

def create_alert_rule(name: str, alert_type: str = "audit_log",
                      description: str = "", conditions: dict = None,
                      actions: list = None, tenant_id: str = None,
                      created_by: str = "") -> str:
    rid = str(uuid.uuid4())[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO alert_rules (id, name, description, alert_type, "
            "conditions, actions, tenant_id, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, name, description, alert_type,
             json.dumps(conditions or {}), json.dumps(actions or []),
             tenant_id, created_by)
        )
    return rid


def get_alert_rule(rule_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM alert_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["conditions"] = json.loads(d["conditions"]) if d["conditions"] else {}
        d["actions"] = json.loads(d["actions"]) if d["actions"] else []
        return d


def list_alert_rules(tenant_id: str = None) -> list[dict]:
    with get_db() as conn:
        if tenant_id:
            rows = conn.execute(
                "SELECT * FROM alert_rules WHERE tenant_id = ? OR tenant_id IS NULL "
                "ORDER BY name",
                (tenant_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM alert_rules ORDER BY name"
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["conditions"] = json.loads(d["conditions"]) if d["conditions"] else {}
            d["actions"] = json.loads(d["actions"]) if d["actions"] else []
            result.append(d)
        return result


def update_alert_rule(rule_id: str, **fields):
    if not fields:
        return
    allowed = {"name", "description", "alert_type", "conditions",
               "actions", "tenant_id", "enabled"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if "conditions" in fields and isinstance(fields["conditions"], dict):
        fields["conditions"] = json.dumps(fields["conditions"])
    if "actions" in fields and isinstance(fields["actions"], list):
        fields["actions"] = json.dumps(fields["actions"])
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [rule_id]
    with get_db() as conn:
        conn.execute(
            f"UPDATE alert_rules SET {set_clause} WHERE id = ?", values
        )


def delete_alert_rule(rule_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM alert_rules WHERE id = ?", (rule_id,)
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Alert History
# ---------------------------------------------------------------------------

def create_alert(rule_id: str, title: str, detail: str = "",
                 severity: str = "info", tenant_id: str = None,
                 tenant_name: str = "", delivered_via: str = "") -> str:
    aid = str(uuid.uuid4())[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO alert_history (id, rule_id, tenant_id, tenant_name, "
            "title, detail, severity, delivered_via) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (aid, rule_id, tenant_id, tenant_name, title, detail,
             severity, delivered_via)
        )
    return aid


def list_alerts(limit: int = 100, status: str = None,
                tenant_id: str = None) -> list[dict]:
    with get_db() as conn:
        query = "SELECT * FROM alert_history WHERE 1=1"
        params = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if tenant_id:
            query += " AND tenant_id = ?"
            params.append(tenant_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def update_alert_status(alert_id: str, status: str):
    with get_db() as conn:
        fields = {"status": status}
        if status == "resolved":
            fields["resolved_at"] = datetime.now().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [alert_id]
        conn.execute(
            f"UPDATE alert_history SET {set_clause} WHERE id = ?", values
        )


def get_alert_counts() -> dict:
    """Get alert counts by severity and status."""
    with get_db() as conn:
        by_severity = conn.execute(
            "SELECT severity, COUNT(*) as count FROM alert_history "
            "WHERE status != 'resolved' GROUP BY severity"
        ).fetchall()
        by_status = conn.execute(
            "SELECT status, COUNT(*) as count FROM alert_history "
            "GROUP BY status"
        ).fetchall()
        return {
            "by_severity": {r["severity"]: r["count"] for r in by_severity},
            "by_status": {r["status"]: r["count"] for r in by_status},
            "total_active": sum(r["count"] for r in by_severity),
        }


# ---------------------------------------------------------------------------
# Secure Scores
# ---------------------------------------------------------------------------

def save_secure_score(tenant_id: str, current_score: float,
                      max_score: float, average_comparative: float = 0,
                      details: dict = None) -> str:
    sid = str(uuid.uuid4())[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO secure_scores (id, tenant_id, current_score, "
            "max_score, average_comparative_score, details) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, tenant_id, current_score, max_score,
             average_comparative, json.dumps(details or {}))
        )
    return sid


def get_latest_secure_score(tenant_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM secure_scores WHERE tenant_id = ? "
            "ORDER BY fetched_at DESC, rowid DESC LIMIT 1",
            (tenant_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["details"] = json.loads(d["details"]) if d["details"] else {}
        return d


def get_all_latest_secure_scores() -> list[dict]:
    """Get the most recent secure score for each tenant."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT s.*, t.name as tenant_name, t.domain as tenant_domain "
            "FROM secure_scores s "
            "JOIN tenants t ON s.tenant_id = t.id "
            "WHERE s.fetched_at = ("
            "  SELECT MAX(s2.fetched_at) FROM secure_scores s2 "
            "  WHERE s2.tenant_id = s.tenant_id"
            ") ORDER BY t.name"
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["details"] = json.loads(d["details"]) if d["details"] else {}
            result.append(d)
        return result
