-- Findings with a lifecycle: first seen, last seen, resolved, accepted risk.
-- One row per (tenant, check, object). A finding that comes back after being
-- resolved is reopened rather than duplicated.
CREATE TABLE findings (
    id             INTEGER PRIMARY KEY,
    tenant_id      INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    rule_key       TEXT NOT NULL,
    object_id      TEXT NOT NULL,
    severity       TEXT NOT NULL,
    subject        TEXT NOT NULL,
    detail         TEXT NOT NULL,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    resolved_at    TEXT,
    reopened_at    TEXT,
    accepted_by    TEXT,
    accepted_at    TEXT,
    accepted_until TEXT,
    accepted_note  TEXT,
    UNIQUE (tenant_id, rule_key, object_id)
);
CREATE INDEX idx_findings_open ON findings(tenant_id, resolved_at);

-- Emailed reports.
CREATE TABLE report_schedules (
    id           INTEGER PRIMARY KEY,
    tenant_id    INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    recipients   TEXT NOT NULL,             -- comma-separated addresses
    day_of_month INTEGER NOT NULL CHECK (day_of_month BETWEEN 1 AND 28),
    hour         INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),
    enabled      INTEGER NOT NULL DEFAULT 1,
    last_period  TEXT,                      -- "2026-10" once that month's report is sent
    created_by   TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE report_deliveries (
    id          INTEGER PRIMARY KEY,
    schedule_id INTEGER REFERENCES report_schedules(id) ON DELETE SET NULL,
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    recipients  TEXT NOT NULL,
    sent_at     TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('sent', 'failed')),
    error       TEXT
);
