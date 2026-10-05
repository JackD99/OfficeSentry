-- What a client was actually sent. Reports are built live from snapshots that
-- get pruned, so every PDF/XLSX attached to a report email that went out is
-- kept here, byte for byte, with its period and recipients. Never pruned
-- automatically; deleting the tenant deletes these rows.
CREATE TABLE issued_reports (
    id           INTEGER PRIMARY KEY,
    tenant_id    INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    schedule_id  INTEGER REFERENCES email_schedules(id) ON DELETE SET NULL,
    kind         TEXT NOT NULL,              -- report key: "review", "evidence", "data:<key>"
    title        TEXT NOT NULL,
    period       TEXT,                       -- the schedule period ("2026-10", "2026-W41", "2026-Q4")
    version      INTEGER NOT NULL DEFAULT 1, -- 2, 3, ... when the same file is sent again for a period
    issued_at    TEXT NOT NULL,
    issued_by    TEXT,                       -- who pressed Send now; NULL for the schedule
    trigger      TEXT NOT NULL,              -- schedule | manual | retry
    recipients   TEXT NOT NULL DEFAULT '',   -- "To" addresses, comma separated
    bcc          TEXT NOT NULL DEFAULT '',   -- the MSP team's hidden copies
    filename     TEXT NOT NULL,
    content_type TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    size         INTEGER NOT NULL,
    data         BLOB NOT NULL
);
CREATE INDEX idx_issued_reports_tenant ON issued_reports(tenant_id, issued_at);
