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
    """Derive a 32-byte Fernet key from the Flask secret using PBKDF2."""
    return hashlib.pbkdf2_hmac('sha256', secret.encode(), b'spo-audit-toolkit-v2', 100_000)


def _derive_key_legacy(secret: str) -> bytes:
    """Legacy SHA256 key derivation (pre-v6 migration)."""
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
    role        TEXT    DEFAULT 'operator',
    allowed_tenant_groups TEXT DEFAULT '[]',
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
    celery_task_id TEXT DEFAULT '',
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

CREATE TABLE IF NOT EXISTS tenant_custom_variables (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    var_key     TEXT NOT NULL,
    var_value   TEXT DEFAULT '',
    created_by  TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    UNIQUE (tenant_id, var_key)
);

CREATE TABLE IF NOT EXISTS cached_tenant_data (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    data_type   TEXT NOT NULL,
    data        TEXT DEFAULT '{}',
    fetched_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS gdap_role_templates (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    roles       TEXT DEFAULT '[]',
    created_by  TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tenant_backups (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    backup_type TEXT NOT NULL DEFAULT 'full',
    components  TEXT DEFAULT '[]',
    status      TEXT NOT NULL DEFAULT 'pending',
    data        TEXT DEFAULT '{}',
    created_by  TEXT NOT NULL,
    started_at  TEXT DEFAULT (datetime('now')),
    completed_at TEXT,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ca_policy_templates (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    policy_json TEXT DEFAULT '{}',
    category    TEXT DEFAULT 'general',
    created_by  TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS incident_cases (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    title       TEXT NOT NULL,
    playbook    TEXT NOT NULL DEFAULT 'general',
    status      TEXT NOT NULL DEFAULT 'open',
    severity    TEXT NOT NULL DEFAULT 'medium',
    target_user TEXT DEFAULT '',
    summary     TEXT DEFAULT '',
    findings    TEXT DEFAULT '[]',
    created_by  TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    closed_at   TEXT,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS incident_notes (
    id          TEXT PRIMARY KEY,
    case_id     TEXT NOT NULL,
    author      TEXT NOT NULL,
    content     TEXT NOT NULL DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (case_id) REFERENCES incident_cases(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS service_health_cache (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    service     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'unknown',
    details     TEXT DEFAULT '{}',
    fetched_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS license_snapshots (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    sku_data    TEXT DEFAULT '[]',
    total_licenses  INTEGER DEFAULT 0,
    assigned_licenses INTEGER DEFAULT 0,
    fetched_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS device_inventory_cache (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    device_count INTEGER DEFAULT 0,
    compliant_count INTEGER DEFAULT 0,
    noncompliant_count INTEGER DEFAULT 0,
    summary     TEXT DEFAULT '{}',
    fetched_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS api_keys (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    key_prefix  TEXT NOT NULL,
    key_hash    TEXT NOT NULL,
    scopes      TEXT DEFAULT '[]',
    role        TEXT DEFAULT 'viewer',
    allowed_tenant_groups TEXT DEFAULT '[]',
    expires_at  TEXT,
    created_by  TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS webhooks (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    url         TEXT NOT NULL,
    secret      TEXT NOT NULL,
    events      TEXT DEFAULT '[]',
    enabled     INTEGER DEFAULT 1,
    created_by  TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    last_delivery_at TEXT,
    last_status TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id          TEXT PRIMARY KEY,
    webhook_id  TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    payload     TEXT DEFAULT '{}',
    status      TEXT DEFAULT 'pending',
    response_code INTEGER,
    attempts    INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now')),
    delivered_at TEXT,
    FOREIGN KEY (webhook_id) REFERENCES webhooks(id) ON DELETE CASCADE
);

-- AD Agent / Hybrid Identity tables (v8)
CREATE TABLE IF NOT EXISTS tenant_ad_config (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL UNIQUE,
    hybrid_detected INTEGER DEFAULT 0,
    hybrid_checked_at TEXT,
    sync_server_names TEXT DEFAULT '[]',
    notes           TEXT DEFAULT '',
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ad_agents (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    agent_name      TEXT NOT NULL DEFAULT 'Default Agent',
    agent_secret_enc TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    enrolled_at     TEXT DEFAULT (datetime('now')),
    last_seen_at    TEXT,
    last_ip         TEXT,
    agent_version   TEXT,
    server_names    TEXT DEFAULT '[]',
    revoked_at      TEXT,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ad_agent_enrollment_tokens (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    token_hash      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    used_at         TEXT,
    used_by_agent_id TEXT,
    created_by      TEXT NOT NULL,
    created_at      TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ad_agent_commands (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    agent_id        TEXT,
    command_type    TEXT NOT NULL,
    parameters      TEXT DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'pending',
    result          TEXT,
    created_by      TEXT NOT NULL,
    created_at      TEXT DEFAULT (datetime('now')),
    picked_up_at    TEXT,
    completed_at    TEXT,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    FOREIGN KEY (agent_id) REFERENCES ad_agents(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS ad_agent_templates (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT DEFAULT '',
    template_data   TEXT DEFAULT '{}',
    created_by      TEXT NOT NULL,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ad_agents_tenant ON ad_agents(tenant_id);
CREATE INDEX IF NOT EXISTS idx_ad_agent_commands_tenant ON ad_agent_commands(tenant_id);
CREATE INDEX IF NOT EXISTS idx_ad_agent_commands_status ON ad_agent_commands(status);
CREATE INDEX IF NOT EXISTS idx_ad_agent_templates_tenant ON ad_agent_templates(tenant_id);
"""


SCHEMA_VERSION = 8

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
    4: """
CREATE TABLE IF NOT EXISTS tenant_custom_variables (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    var_key     TEXT NOT NULL,
    var_value   TEXT DEFAULT '',
    created_by  TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    UNIQUE (tenant_id, var_key)
);

CREATE TABLE IF NOT EXISTS cached_tenant_data (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    data_type   TEXT NOT NULL,
    data        TEXT DEFAULT '{}',
    fetched_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS gdap_role_templates (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    roles       TEXT DEFAULT '[]',
    created_by  TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tenant_backups (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    backup_type TEXT NOT NULL DEFAULT 'full',
    components  TEXT DEFAULT '[]',
    status      TEXT NOT NULL DEFAULT 'pending',
    data        TEXT DEFAULT '{}',
    created_by  TEXT NOT NULL,
    started_at  TEXT DEFAULT (datetime('now')),
    completed_at TEXT,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ca_policy_templates (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    policy_json TEXT DEFAULT '{}',
    category    TEXT DEFAULT 'general',
    created_by  TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);
""",
    5: """
CREATE TABLE IF NOT EXISTS incident_cases (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    title       TEXT NOT NULL,
    playbook    TEXT NOT NULL DEFAULT 'general',
    status      TEXT NOT NULL DEFAULT 'open',
    severity    TEXT NOT NULL DEFAULT 'medium',
    target_user TEXT DEFAULT '',
    summary     TEXT DEFAULT '',
    findings    TEXT DEFAULT '[]',
    created_by  TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    closed_at   TEXT,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS incident_notes (
    id          TEXT PRIMARY KEY,
    case_id     TEXT NOT NULL,
    author      TEXT NOT NULL,
    content     TEXT NOT NULL DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (case_id) REFERENCES incident_cases(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS service_health_cache (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    service     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'unknown',
    details     TEXT DEFAULT '{}',
    fetched_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS license_snapshots (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    sku_data    TEXT DEFAULT '[]',
    total_licenses  INTEGER DEFAULT 0,
    assigned_licenses INTEGER DEFAULT 0,
    fetched_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS device_inventory_cache (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    device_count INTEGER DEFAULT 0,
    compliant_count INTEGER DEFAULT 0,
    noncompliant_count INTEGER DEFAULT 0,
    summary     TEXT DEFAULT '{}',
    fetched_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);
""",
    6: """
CREATE INDEX IF NOT EXISTS idx_jobs_tenant_id ON jobs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_audit_log_tenant_id ON audit_log(tenant_id);
CREATE INDEX IF NOT EXISTS idx_cached_tenant_data_lookup ON cached_tenant_data(tenant_id, data_type);
CREATE INDEX IF NOT EXISTS idx_bpa_results_tenant ON bpa_results(tenant_id);
CREATE INDEX IF NOT EXISTS idx_incident_cases_tenant ON incident_cases(tenant_id);
CREATE INDEX IF NOT EXISTS idx_service_health_cache_tenant ON service_health_cache(tenant_id);
CREATE INDEX IF NOT EXISTS idx_license_snapshots_tenant ON license_snapshots(tenant_id);
""",
    7: """
ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'operator';
ALTER TABLE users ADD COLUMN allowed_tenant_groups TEXT DEFAULT '[]';
UPDATE users SET role = 'super_admin' WHERE is_admin = 1;
UPDATE users SET role = 'operator' WHERE is_admin = 0;

ALTER TABLE jobs ADD COLUMN celery_task_id TEXT DEFAULT '';

CREATE TABLE IF NOT EXISTS api_keys (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    key_prefix  TEXT NOT NULL,
    key_hash    TEXT NOT NULL,
    scopes      TEXT DEFAULT '[]',
    role        TEXT DEFAULT 'viewer',
    allowed_tenant_groups TEXT DEFAULT '[]',
    expires_at  TEXT,
    created_by  TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS webhooks (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    url         TEXT NOT NULL,
    secret      TEXT NOT NULL,
    events      TEXT DEFAULT '[]',
    enabled     INTEGER DEFAULT 1,
    created_by  TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    last_delivery_at TEXT,
    last_status TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id          TEXT PRIMARY KEY,
    webhook_id  TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    payload     TEXT DEFAULT '{}',
    status      TEXT DEFAULT 'pending',
    response_code INTEGER,
    attempts    INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now')),
    delivered_at TEXT,
    FOREIGN KEY (webhook_id) REFERENCES webhooks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_api_keys_prefix ON api_keys(key_prefix);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_webhook ON webhook_deliveries(webhook_id);
""",
    8: """
CREATE TABLE IF NOT EXISTS tenant_ad_config (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL UNIQUE,
    hybrid_detected INTEGER DEFAULT 0,
    hybrid_checked_at TEXT,
    sync_server_names TEXT DEFAULT '[]',
    notes           TEXT DEFAULT '',
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ad_agents (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    agent_name      TEXT NOT NULL DEFAULT 'Default Agent',
    agent_secret_enc TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    enrolled_at     TEXT DEFAULT (datetime('now')),
    last_seen_at    TEXT,
    last_ip         TEXT,
    agent_version   TEXT,
    server_names    TEXT DEFAULT '[]',
    revoked_at      TEXT,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ad_agent_enrollment_tokens (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    token_hash      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    used_at         TEXT,
    used_by_agent_id TEXT,
    created_by      TEXT NOT NULL,
    created_at      TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ad_agent_commands (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    agent_id        TEXT,
    command_type    TEXT NOT NULL,
    parameters      TEXT DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'pending',
    result          TEXT,
    created_by      TEXT NOT NULL,
    created_at      TEXT DEFAULT (datetime('now')),
    picked_up_at    TEXT,
    completed_at    TEXT,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    FOREIGN KEY (agent_id) REFERENCES ad_agents(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS ad_agent_templates (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT DEFAULT '',
    template_data   TEXT DEFAULT '{}',
    created_by      TEXT NOT NULL,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ad_agents_tenant ON ad_agents(tenant_id);
CREATE INDEX IF NOT EXISTS idx_ad_agent_commands_tenant ON ad_agent_commands(tenant_id);
CREATE INDEX IF NOT EXISTS idx_ad_agent_commands_status ON ad_agent_commands(status);
CREATE INDEX IF NOT EXISTS idx_ad_agent_templates_tenant ON ad_agent_templates(tenant_id);
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


def migrate_encryption(secret: str):
    """One-time migration: re-encrypt PFX passwords from SHA256 to PBKDF2 key derivation."""
    from cryptography.fernet import Fernet, InvalidToken

    old_key = base64.urlsafe_b64encode(_derive_key_legacy(secret))
    new_key = base64.urlsafe_b64encode(_derive_key(secret))

    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, pfx_password_enc FROM tenants WHERE pfx_password_enc != ''"
        ).fetchall()
        for row in rows:
            try:
                plaintext = Fernet(old_key).decrypt(row["pfx_password_enc"].encode()).decode()
                new_enc = Fernet(new_key).encrypt(plaintext.encode()).decode()
                conn.execute(
                    "UPDATE tenants SET pfx_password_enc = ? WHERE id = ?",
                    (new_enc, row["id"])
                )
            except (InvalidToken, Exception):
                pass  # Already migrated or corrupt


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
    # Auto-set role based on is_admin for backward compatibility
    role = "super_admin" if is_admin else "operator"
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, display_name, is_admin, role) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, hash_password(password), display_name or username, is_admin, role)
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
            "SELECT id, username, display_name, is_admin, role, "
            "allowed_tenant_groups, created_at "
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
    allowed = {"display_name", "is_admin", "role", "allowed_tenant_groups"}
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


# ---------------------------------------------------------------------------
# Tenant Custom Variables
# ---------------------------------------------------------------------------

def set_tenant_variable(tenant_id: str, var_key: str, var_value: str,
                        created_by: str = "") -> str:
    vid = str(uuid.uuid4())[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO tenant_custom_variables (id, tenant_id, var_key, var_value, created_by) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, var_key) DO UPDATE SET "
            "var_value = excluded.var_value, updated_at = datetime('now')",
            (vid, tenant_id, var_key, var_value, created_by)
        )
    return vid


def get_tenant_variables(tenant_id: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tenant_custom_variables WHERE tenant_id = ? ORDER BY var_key",
            (tenant_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def delete_tenant_variable(tenant_id: str, var_key: str) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM tenant_custom_variables WHERE tenant_id = ? AND var_key = ?",
            (tenant_id, var_key)
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Cached Tenant Data (domains, auth methods, CA policies, etc.)
# ---------------------------------------------------------------------------

def save_cached_data(tenant_id: str, data_type: str, data: dict) -> str:
    cid = str(uuid.uuid4())[:12]
    with get_db() as conn:
        # Remove old cache for this type
        conn.execute(
            "DELETE FROM cached_tenant_data WHERE tenant_id = ? AND data_type = ?",
            (tenant_id, data_type)
        )
        conn.execute(
            "INSERT INTO cached_tenant_data (id, tenant_id, data_type, data) "
            "VALUES (?, ?, ?, ?)",
            (cid, tenant_id, data_type, json.dumps(data))
        )
    return cid


def get_cached_data(tenant_id: str, data_type: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM cached_tenant_data WHERE tenant_id = ? AND data_type = ? "
            "ORDER BY fetched_at DESC LIMIT 1",
            (tenant_id, data_type)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["data"] = json.loads(d["data"]) if d["data"] else {}
        return d


def list_cached_data(tenant_id: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM cached_tenant_data WHERE tenant_id = ? ORDER BY data_type",
            (tenant_id,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d["data"]) if d["data"] else {}
            result.append(d)
        return result


# ---------------------------------------------------------------------------
# GDAP Role Templates
# ---------------------------------------------------------------------------

def create_gdap_role_template(name: str, description: str = "",
                               roles: list = None,
                               created_by: str = "") -> str:
    rid = str(uuid.uuid4())[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO gdap_role_templates (id, name, description, roles, created_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (rid, name, description, json.dumps(roles or []), created_by)
        )
    return rid


def get_gdap_role_template(template_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM gdap_role_templates WHERE id = ?", (template_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["roles"] = json.loads(d["roles"]) if d["roles"] else []
        return d


def list_gdap_role_templates() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM gdap_role_templates ORDER BY name"
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["roles"] = json.loads(d["roles"]) if d["roles"] else []
            result.append(d)
        return result


def update_gdap_role_template(template_id: str, **fields):
    if not fields:
        return
    allowed = {"name", "description", "roles"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if "roles" in fields and isinstance(fields["roles"], list):
        fields["roles"] = json.dumps(fields["roles"])
    fields["updated_at"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [template_id]
    with get_db() as conn:
        conn.execute(
            f"UPDATE gdap_role_templates SET {set_clause} WHERE id = ?", values
        )


def delete_gdap_role_template(template_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM gdap_role_templates WHERE id = ?", (template_id,)
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Tenant Backups
# ---------------------------------------------------------------------------

def create_tenant_backup(tenant_id: str, backup_type: str = "full",
                          components: list = None,
                          created_by: str = "") -> str:
    bid = str(uuid.uuid4())[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO tenant_backups (id, tenant_id, backup_type, components, "
            "status, created_by) VALUES (?, ?, ?, ?, 'pending', ?)",
            (bid, tenant_id, backup_type, json.dumps(components or []), created_by)
        )
    return bid


def get_tenant_backup(backup_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM tenant_backups WHERE id = ?", (backup_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["components"] = json.loads(d["components"]) if d["components"] else []
        d["data"] = json.loads(d["data"]) if d["data"] else {}
        return d


def list_tenant_backups(tenant_id: str = None) -> list[dict]:
    with get_db() as conn:
        if tenant_id:
            rows = conn.execute(
                "SELECT b.*, t.name as tenant_name FROM tenant_backups b "
                "JOIN tenants t ON b.tenant_id = t.id "
                "WHERE b.tenant_id = ? ORDER BY b.started_at DESC",
                (tenant_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT b.*, t.name as tenant_name FROM tenant_backups b "
                "JOIN tenants t ON b.tenant_id = t.id "
                "ORDER BY b.started_at DESC"
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["components"] = json.loads(d["components"]) if d["components"] else []
            d["data"] = json.loads(d["data"]) if d["data"] else {}
            result.append(d)
        return result


def update_tenant_backup(backup_id: str, **fields):
    if not fields:
        return
    allowed = {"status", "data", "completed_at"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if "data" in fields and isinstance(fields["data"], dict):
        fields["data"] = json.dumps(fields["data"])
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [backup_id]
    with get_db() as conn:
        conn.execute(
            f"UPDATE tenant_backups SET {set_clause} WHERE id = ?", values
        )


def delete_tenant_backup(backup_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM tenant_backups WHERE id = ?", (backup_id,)
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Conditional Access Policy Templates
# ---------------------------------------------------------------------------

def create_ca_template(name: str, description: str = "",
                        policy_json: dict = None, category: str = "general",
                        created_by: str = "") -> str:
    tid = str(uuid.uuid4())[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO ca_policy_templates (id, name, description, policy_json, "
            "category, created_by) VALUES (?, ?, ?, ?, ?, ?)",
            (tid, name, description, json.dumps(policy_json or {}), category, created_by)
        )
    return tid


def get_ca_template(template_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM ca_policy_templates WHERE id = ?", (template_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["policy_json"] = json.loads(d["policy_json"]) if d["policy_json"] else {}
        return d


def list_ca_templates() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM ca_policy_templates ORDER BY category, name"
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["policy_json"] = json.loads(d["policy_json"]) if d["policy_json"] else {}
            result.append(d)
        return result


def update_ca_template(template_id: str, **fields):
    if not fields:
        return
    allowed = {"name", "description", "policy_json", "category"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if "policy_json" in fields and isinstance(fields["policy_json"], dict):
        fields["policy_json"] = json.dumps(fields["policy_json"])
    fields["updated_at"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [template_id]
    with get_db() as conn:
        conn.execute(
            f"UPDATE ca_policy_templates SET {set_clause} WHERE id = ?", values
        )


def delete_ca_template(template_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM ca_policy_templates WHERE id = ?", (template_id,)
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Incident Cases
# ---------------------------------------------------------------------------

def create_incident_case(tenant_id: str, title: str, playbook: str,
                         severity: str, created_by: str,
                         target_user: str = "", summary: str = "") -> str:
    case_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO incident_cases "
            "(id, tenant_id, title, playbook, severity, target_user, summary, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (case_id, tenant_id, title, playbook, severity,
             target_user, summary, created_by),
        )
    return case_id


def get_incident_case(case_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM incident_cases WHERE id = ?", (case_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["findings"] = json.loads(d["findings"]) if d["findings"] else []
        return d


def list_incident_cases(tenant_id: str = None, status: str = None) -> list:
    with get_db() as conn:
        sql = "SELECT * FROM incident_cases WHERE 1=1"
        params = []
        if tenant_id:
            sql += " AND tenant_id = ?"
            params.append(tenant_id)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        rows = conn.execute(sql, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["findings"] = json.loads(d["findings"]) if d["findings"] else []
            result.append(d)
        return result


def update_incident_case(case_id: str, **fields):
    if not fields:
        return
    allowed = {"title", "status", "severity", "summary", "findings", "closed_at"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if "findings" in fields and isinstance(fields["findings"], list):
        fields["findings"] = json.dumps(fields["findings"])
    fields["updated_at"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [case_id]
    with get_db() as conn:
        conn.execute(
            f"UPDATE incident_cases SET {set_clause} WHERE id = ?", values
        )


def add_incident_note(case_id: str, author: str, content: str) -> str:
    note_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO incident_notes (id, case_id, author, content) "
            "VALUES (?, ?, ?, ?)",
            (note_id, case_id, author, content),
        )
    return note_id


def list_incident_notes(case_id: str) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM incident_notes WHERE case_id = ? ORDER BY created_at ASC",
            (case_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Service Health Cache
# ---------------------------------------------------------------------------

def save_service_health(tenant_id: str, service: str, status: str,
                        details: dict = None) -> str:
    entry_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO service_health_cache "
            "(id, tenant_id, service, status, details) VALUES (?, ?, ?, ?, ?)",
            (entry_id, tenant_id, service, status,
             json.dumps(details or {})),
        )
    return entry_id


def get_service_health(tenant_id: str) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM service_health_cache WHERE tenant_id = ? "
            "ORDER BY fetched_at DESC",
            (tenant_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["details"] = json.loads(d["details"]) if d["details"] else {}
            result.append(d)
        return result


# ---------------------------------------------------------------------------
# License Snapshots
# ---------------------------------------------------------------------------

def save_license_snapshot(tenant_id: str, sku_data: list,
                          total: int, assigned: int) -> str:
    snap_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO license_snapshots "
            "(id, tenant_id, sku_data, total_licenses, assigned_licenses) "
            "VALUES (?, ?, ?, ?, ?)",
            (snap_id, tenant_id, json.dumps(sku_data), total, assigned),
        )
    return snap_id


def get_latest_license_snapshot(tenant_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM license_snapshots WHERE tenant_id = ? "
            "ORDER BY fetched_at DESC LIMIT 1",
            (tenant_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["sku_data"] = json.loads(d["sku_data"]) if d["sku_data"] else []
        return d


# ---------------------------------------------------------------------------
# Device Inventory Cache
# ---------------------------------------------------------------------------

def save_device_inventory(tenant_id: str, device_count: int,
                          compliant: int, noncompliant: int,
                          summary: dict = None) -> str:
    inv_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO device_inventory_cache "
            "(id, tenant_id, device_count, compliant_count, "
            "noncompliant_count, summary) VALUES (?, ?, ?, ?, ?, ?)",
            (inv_id, tenant_id, device_count, compliant, noncompliant,
             json.dumps(summary or {})),
        )
    return inv_id


def get_latest_device_inventory(tenant_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM device_inventory_cache WHERE tenant_id = ? "
            "ORDER BY fetched_at DESC LIMIT 1",
            (tenant_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["summary"] = json.loads(d["summary"]) if d["summary"] else {}
        return d


# ---------------------------------------------------------------------------
# Celery Task ID tracking
# ---------------------------------------------------------------------------

def update_job_task_id(job_id: str, task_id: str):
    """Store the Celery task ID for a job (used for revocation)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE jobs SET celery_task_id = ? WHERE id = ?",
            (task_id, job_id),
        )


def get_job_task_id(job_id: str) -> str:
    """Retrieve the Celery task ID for a job."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT celery_task_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return row["celery_task_id"] if row and row["celery_task_id"] else ""


# ---------------------------------------------------------------------------
# Report Schedule helpers
# ---------------------------------------------------------------------------

def get_due_schedules() -> list[dict]:
    """Return enabled report schedules that are due for execution."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM report_schedules "
            "WHERE enabled = 1 AND (next_run IS NULL OR next_run <= datetime('now'))"
        ).fetchall()
        return [dict(r) for r in rows]


def update_schedule_run(schedule_id: str):
    """Update a schedule's last_run and calculate next_run."""
    with get_db() as conn:
        schedule = conn.execute(
            "SELECT * FROM report_schedules WHERE id = ?", (schedule_id,)
        ).fetchone()
        if not schedule:
            return
        freq = schedule["frequency"]
        # Calculate next run offset based on frequency
        if freq == "daily":
            offset = "+1 day"
        elif freq == "weekly":
            offset = "+7 days"
        elif freq == "monthly":
            offset = "+30 days"
        else:
            offset = "+7 days"
        conn.execute(
            "UPDATE report_schedules SET last_run = datetime('now'), "
            f"next_run = datetime('now', '{offset}') WHERE id = ?",
            (schedule_id,),
        )


# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------

def create_api_key(name: str, role: str = "viewer",
                   allowed_tenant_groups: str = "[]",
                   scopes: str = "[]", expires_at: str = None,
                   created_by: str = "") -> tuple[str, str]:
    """Create a new API key. Returns (key_id, raw_key).

    The raw key is only returned once — it is stored as a SHA256 hash.
    """
    import secrets as _secrets
    key_id = uuid.uuid4().hex[:12]
    raw_key = "spat_" + _secrets.token_hex(32)
    key_prefix = raw_key[:13]  # "spat_" + 8 hex chars
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO api_keys (id, name, key_prefix, key_hash, scopes, "
            "role, allowed_tenant_groups, expires_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key_id, name, key_prefix, key_hash, scopes, role,
             allowed_tenant_groups, expires_at, created_by),
        )
    return key_id, raw_key


def validate_api_key(raw_key: str) -> dict | None:
    """Validate an API key and return its record if valid, or None."""
    if not raw_key or not raw_key.startswith("spat_"):
        return None
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        # Check expiration
        if d.get("expires_at"):
            if d["expires_at"] < datetime.now().isoformat():
                return None
        # Update last_used_at
        conn.execute(
            "UPDATE api_keys SET last_used_at = datetime('now') WHERE id = ?",
            (d["id"],),
        )
        return d


def list_api_keys() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, key_prefix, scopes, role, "
            "allowed_tenant_groups, expires_at, created_by, "
            "created_at, last_used_at FROM api_keys ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def delete_api_key(key_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

def create_webhook(name: str, url: str, secret: str, events: str = "[]",
                   created_by: str = "") -> str:
    wh_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO webhooks (id, name, url, secret, events, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (wh_id, name, url, secret, events, created_by),
        )
    return wh_id


def get_webhook(webhook_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM webhooks WHERE id = ?", (webhook_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["events"] = json.loads(d["events"]) if d["events"] else []
        return d


def list_webhooks() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM webhooks ORDER BY created_at DESC"
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["events"] = json.loads(d["events"]) if d["events"] else []
            result.append(d)
        return result


def update_webhook(webhook_id: str, **fields):
    if not fields:
        return
    allowed = {"name", "url", "secret", "events", "enabled"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [webhook_id]
    with get_db() as conn:
        conn.execute(
            f"UPDATE webhooks SET {set_clause} WHERE id = ?", values
        )


def delete_webhook(webhook_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))
        return cur.rowcount > 0


def toggle_webhook(webhook_id: str) -> bool | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT enabled FROM webhooks WHERE id = ?", (webhook_id,)
        ).fetchone()
        if not row:
            return None
        new_val = 0 if row["enabled"] else 1
        conn.execute(
            "UPDATE webhooks SET enabled = ? WHERE id = ?",
            (new_val, webhook_id),
        )
        return bool(new_val)


def update_webhook_delivery_status(webhook_id: str, status: str):
    """Update the last delivery status on a webhook."""
    with get_db() as conn:
        conn.execute(
            "UPDATE webhooks SET last_delivery_at = datetime('now'), "
            "last_status = ? WHERE id = ?",
            (status, webhook_id),
        )


def create_webhook_delivery(webhook_id: str, event_type: str,
                            payload: str = "{}") -> str:
    d_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO webhook_deliveries "
            "(id, webhook_id, event_type, payload) VALUES (?, ?, ?, ?)",
            (d_id, webhook_id, event_type, payload),
        )
    return d_id


def update_webhook_delivery(delivery_id: str, status: str,
                            response_code: int = None, attempts: int = 1):
    with get_db() as conn:
        if status == "delivered":
            conn.execute(
                "UPDATE webhook_deliveries SET status = ?, response_code = ?, "
                "attempts = ?, delivered_at = datetime('now') WHERE id = ?",
                (status, response_code, attempts, delivery_id),
            )
        else:
            conn.execute(
                "UPDATE webhook_deliveries SET status = ?, response_code = ?, "
                "attempts = ? WHERE id = ?",
                (status, response_code, attempts, delivery_id),
            )


def list_webhook_deliveries(webhook_id: str, limit: int = 50) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM webhook_deliveries WHERE webhook_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (webhook_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# AD Agent — tenant hybrid config
# ---------------------------------------------------------------------------

def get_tenant_ad_config(tenant_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM tenant_ad_config WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["sync_server_names"] = json.loads(d["sync_server_names"]) if d["sync_server_names"] else []
        return d


def upsert_tenant_ad_config(tenant_id: str, **fields) -> str:
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM tenant_ad_config WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        if "sync_server_names" in fields and isinstance(fields["sync_server_names"], list):
            fields["sync_server_names"] = json.dumps(fields["sync_server_names"])
        if existing:
            set_clause = ", ".join(f"{k} = ?" for k in fields)
            values = list(fields.values()) + [tenant_id]
            conn.execute(
                f"UPDATE tenant_ad_config SET {set_clause} WHERE tenant_id = ?",
                values,
            )
            return existing["id"]
        else:
            row_id = uuid.uuid4().hex[:12]
            fields["id"] = row_id
            fields["tenant_id"] = tenant_id
            cols = ", ".join(fields.keys())
            placeholders = ", ".join("?" for _ in fields)
            conn.execute(
                f"INSERT INTO tenant_ad_config ({cols}) VALUES ({placeholders})",
                list(fields.values()),
            )
            return row_id


# ---------------------------------------------------------------------------
# AD Agent — agent instances
# ---------------------------------------------------------------------------

def create_ad_agent(tenant_id: str, agent_name: str,
                    agent_secret_enc: str) -> str:
    agent_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO ad_agents (id, tenant_id, agent_name, agent_secret_enc) "
            "VALUES (?, ?, ?, ?)",
            (agent_id, tenant_id, agent_name, agent_secret_enc),
        )
    return agent_id


def get_ad_agent(agent_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM ad_agents WHERE id = ?", (agent_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["server_names"] = json.loads(d["server_names"]) if d["server_names"] else []
        return d


def get_ad_agent_for_tenant(tenant_id: str) -> dict | None:
    """Get the active agent for a tenant (most recently enrolled)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM ad_agents WHERE tenant_id = ? AND status = 'active' "
            "ORDER BY enrolled_at DESC LIMIT 1",
            (tenant_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["server_names"] = json.loads(d["server_names"]) if d["server_names"] else []
        return d


def list_ad_agents(tenant_id: str = None) -> list[dict]:
    with get_db() as conn:
        if tenant_id:
            rows = conn.execute(
                "SELECT * FROM ad_agents WHERE tenant_id = ? ORDER BY enrolled_at DESC",
                (tenant_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ad_agents ORDER BY enrolled_at DESC"
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["server_names"] = json.loads(d["server_names"]) if d["server_names"] else []
            result.append(d)
        return result


def update_ad_agent(agent_id: str, **fields):
    if not fields:
        return
    allowed = {
        "agent_name", "status", "last_seen_at", "last_ip",
        "agent_version", "server_names", "revoked_at", "agent_secret_enc",
    }
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    if "server_names" in fields and isinstance(fields["server_names"], list):
        fields["server_names"] = json.dumps(fields["server_names"])
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [agent_id]
    with get_db() as conn:
        conn.execute(
            f"UPDATE ad_agents SET {set_clause} WHERE id = ?", values
        )


def revoke_ad_agent(agent_id: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE ad_agents SET status = 'revoked', "
            "revoked_at = datetime('now') WHERE id = ?",
            (agent_id,),
        )


def delete_ad_agent(agent_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM ad_agents WHERE id = ?", (agent_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# AD Agent — enrollment tokens
# ---------------------------------------------------------------------------

def create_enrollment_token(tenant_id: str, token_hash: str,
                            expires_at: str, created_by: str) -> str:
    token_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO ad_agent_enrollment_tokens "
            "(id, tenant_id, token_hash, expires_at, created_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (token_id, tenant_id, token_hash, expires_at, created_by),
        )
    return token_id


def validate_enrollment_token(tenant_id: str, token_hash: str) -> dict | None:
    """Find a valid (unused, non-expired) enrollment token for a tenant."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM ad_agent_enrollment_tokens "
            "WHERE tenant_id = ? AND token_hash = ? "
            "AND used_at IS NULL AND expires_at > datetime('now')",
            (tenant_id, token_hash),
        ).fetchone()
        return dict(row) if row else None


def consume_enrollment_token(token_id: str, agent_id: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE ad_agent_enrollment_tokens SET used_at = datetime('now'), "
            "used_by_agent_id = ? WHERE id = ?",
            (agent_id, token_id),
        )


def list_enrollment_tokens(tenant_id: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, tenant_id, expires_at, used_at, used_by_agent_id, "
            "created_by, created_at FROM ad_agent_enrollment_tokens "
            "WHERE tenant_id = ? ORDER BY created_at DESC",
            (tenant_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_enrollment_token(token_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM ad_agent_enrollment_tokens WHERE id = ?", (token_id,)
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# AD Agent — command queue
# ---------------------------------------------------------------------------

VALID_COMMAND_TYPES = {
    "lookup_user", "update_user", "add_group_member",
    "remove_group_member", "apply_template", "ping",
}


def create_agent_command(tenant_id: str, command_type: str,
                         parameters: dict, created_by: str,
                         agent_id: str = None) -> str:
    cmd_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO ad_agent_commands "
            "(id, tenant_id, agent_id, command_type, parameters, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (cmd_id, tenant_id, agent_id, command_type,
             json.dumps(parameters), created_by),
        )
    return cmd_id


def get_agent_command(command_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM ad_agent_commands WHERE id = ?", (command_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["parameters"] = json.loads(d["parameters"]) if d["parameters"] else {}
        d["result"] = json.loads(d["result"]) if d["result"] else None
        return d


def poll_pending_commands(tenant_id: str, agent_id: str,
                          limit: int = 10) -> list[dict]:
    """Get pending commands for an agent and mark them as running."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM ad_agent_commands "
            "WHERE tenant_id = ? AND status = 'pending' "
            "AND (agent_id IS NULL OR agent_id = ?) "
            "ORDER BY created_at ASC LIMIT ?",
            (tenant_id, agent_id, limit),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["parameters"] = json.loads(d["parameters"]) if d["parameters"] else {}
            conn.execute(
                "UPDATE ad_agent_commands SET status = 'running', "
                "agent_id = ?, picked_up_at = datetime('now') WHERE id = ?",
                (agent_id, d["id"]),
            )
            result.append(d)
        return result


def complete_agent_command(command_id: str, result: dict, status: str = "completed"):
    if status not in ("completed", "failed"):
        status = "completed"
    with get_db() as conn:
        conn.execute(
            "UPDATE ad_agent_commands SET status = ?, result = ?, "
            "completed_at = datetime('now') WHERE id = ?",
            (status, json.dumps(result), command_id),
        )


def list_agent_commands(tenant_id: str, limit: int = 50,
                        status: str = None) -> list[dict]:
    with get_db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM ad_agent_commands "
                "WHERE tenant_id = ? AND status = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (tenant_id, status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ad_agent_commands WHERE tenant_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["parameters"] = json.loads(d["parameters"]) if d["parameters"] else {}
            d["result"] = json.loads(d["result"]) if d["result"] else None
            result.append(d)
        return result


# ---------------------------------------------------------------------------
# AD Agent — user templates
# ---------------------------------------------------------------------------

def create_agent_template(tenant_id: str, name: str, description: str,
                          template_data: dict, created_by: str) -> str:
    tmpl_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO ad_agent_templates "
            "(id, tenant_id, name, description, template_data, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tmpl_id, tenant_id, name, description,
             json.dumps(template_data), created_by),
        )
    return tmpl_id


def get_agent_template(template_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM ad_agent_templates WHERE id = ?", (template_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["template_data"] = json.loads(d["template_data"]) if d["template_data"] else {}
        return d


def list_agent_templates(tenant_id: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM ad_agent_templates WHERE tenant_id = ? "
            "ORDER BY name",
            (tenant_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["template_data"] = json.loads(d["template_data"]) if d["template_data"] else {}
            result.append(d)
        return result


def update_agent_template(template_id: str, **fields):
    if not fields:
        return
    allowed = {"name", "description", "template_data"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    if "template_data" in fields and isinstance(fields["template_data"], dict):
        fields["template_data"] = json.dumps(fields["template_data"])
    fields["updated_at"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [template_id]
    with get_db() as conn:
        conn.execute(
            f"UPDATE ad_agent_templates SET {set_clause} WHERE id = ?", values
        )


def delete_agent_template(template_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM ad_agent_templates WHERE id = ?", (template_id,)
        )
        return cur.rowcount > 0
