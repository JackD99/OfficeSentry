"""
portal/database.py — SQLite persistence layer for the M365 Audit Portal.
Manages tenants, jobs, audit trail, and operator accounts.
"""

import json
import sqlite3
import uuid
import hashlib
import base64
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).parent.parent / "data" / "portal.db"


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
    app_display_name TEXT DEFAULT 'M365-Audit-App',
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
"""


def init_db():
    with get_db() as conn:
        conn.executescript(SCHEMA)


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


def update_tenant(tenant_id: str, **fields):
    if not fields:
        return
    fields["updated_at"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [tenant_id]
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
