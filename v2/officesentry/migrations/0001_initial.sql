-- Office Sentry v2 initial schema.
-- Timestamps are ISO-8601 UTC strings ("2026-10-02T13:00:00Z").

CREATE TABLE users (
    id              INTEGER PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('admin', 'analyst', 'client')),
    all_tenants     INTEGER NOT NULL DEFAULT 0,
    disabled        INTEGER NOT NULL DEFAULT 0,
    session_version INTEGER NOT NULL DEFAULT 1,
    failed_logins   INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT,
    created_at      TEXT NOT NULL,
    last_login_at   TEXT
);

-- Explicit tenant grants. Deny by default: a non-admin without all_tenants
-- sees only tenants listed here.
CREATE TABLE user_tenants (
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, tenant_id)
);

-- An Entra app registration Office Sentry signs in as. The private key is
-- stored encrypted; nothing credential-related lives on disk as a file.
CREATE TABLE app_connections (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    client_id    TEXT,
    multi_tenant INTEGER NOT NULL DEFAULT 1,
    cert_pem     TEXT NOT NULL,
    key_enc      TEXT NOT NULL,
    thumbprint   TEXT NOT NULL,
    not_after    TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE tenants (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    tenant_ref    TEXT NOT NULL UNIQUE COLLATE NOCASE,  -- GUID or initial domain
    connection_id INTEGER REFERENCES app_connections(id) ON DELETE SET NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    consented_at  TEXT,
    capabilities  TEXT NOT NULL DEFAULT '{}',          -- JSON, from the tenant_profile collector
    created_at    TEXT NOT NULL
);

-- One row per collector execution. Every run is a snapshot.
CREATE TABLE runs (
    id            INTEGER PRIMARY KEY,
    tenant_id     INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    collector     TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'partial', 'failed')),
    trigger       TEXT NOT NULL CHECK (trigger IN ('manual', 'schedule')),
    requested_by  TEXT,
    scheduled_for TEXT,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    error         TEXT,
    truncated     INTEGER NOT NULL DEFAULT 0,
    object_count  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_runs_status ON runs(status, created_at);
CREATE INDEX idx_runs_tenant ON runs(tenant_id, collector, created_at);

-- Append-only run log (one row per line, never rewritten).
CREATE TABLE run_events (
    id      INTEGER PRIMARY KEY,
    run_id  INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX idx_run_events_run ON run_events(run_id, id);

-- Collected objects: one row per user / role / policy / sku ... per run.
CREATE TABLE objects (
    run_id    INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL,
    object_id TEXT NOT NULL,
    data      TEXT NOT NULL,
    PRIMARY KEY (run_id, kind, object_id)
);

-- Numbers for trends and cross-tenant rollups. Kept after objects are pruned.
CREATE TABLE metrics (
    run_id      INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    value       REAL NOT NULL,
    captured_at TEXT NOT NULL
);
CREATE INDEX idx_metrics_lookup ON metrics(tenant_id, key, captured_at);

CREATE TABLE audit_log (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    username  TEXT,
    action    TEXT NOT NULL,
    tenant_id INTEGER,
    detail    TEXT
);
CREATE INDEX idx_audit_ts ON audit_log(ts);
