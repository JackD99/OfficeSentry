-- Month-end reporting: report plans applied to many tenants at once, a monthly
-- review queue (draft, in review, approved, sent), the technical lead's
-- commentary, recommendations and notes, and approval that freezes the files
-- into issued_reports before they are sent. Reporting only: nothing here
-- touches a tenant.

-- One MSP-level plan: which reports go out monthly, when, with what subject and
-- message ({client} and {month} are filled in), and whether they send on their
-- timer ('auto') or wait for approval ('approval'). delivery 'portal' never
-- emails: approving publishes the files to the tenant's report history.
CREATE TABLE report_plans (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    reports      TEXT NOT NULL DEFAULT '["review"]',   -- JSON list of report keys (delivery.catalog)
    include_xlsx INTEGER NOT NULL DEFAULT 1,
    day_of_month INTEGER NOT NULL DEFAULT 1 CHECK (day_of_month BETWEEN 1 AND 28),
    hour         INTEGER NOT NULL DEFAULT 8 CHECK (hour BETWEEN 0 AND 23),
    timezone     TEXT NOT NULL DEFAULT '',
    subject      TEXT NOT NULL DEFAULT '',
    message      TEXT NOT NULL DEFAULT '',
    mode         TEXT NOT NULL DEFAULT 'auto' CHECK (mode IN ('auto', 'approval')),
    delivery     TEXT NOT NULL DEFAULT 'email' CHECK (delivery IN ('email', 'portal')),
    bcc_team     INTEGER NOT NULL DEFAULT 0,
    created_by   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT
);

-- A tenant is on at most one plan. The plan's per-tenant schedule is an
-- ordinary email_schedules row, so the existing sender sends it.
CREATE TABLE report_plan_tenants (
    tenant_id   INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    plan_id     INTEGER NOT NULL REFERENCES report_plans(id) ON DELETE CASCADE,
    schedule_id INTEGER REFERENCES email_schedules(id) ON DELETE SET NULL,
    added_by    TEXT,
    added_at    TEXT NOT NULL
);
CREATE INDEX idx_report_plan_tenants_plan ON report_plan_tenants(plan_id);

-- Plan schedules carry the plan's subject and wait for approval when asked to.
ALTER TABLE email_schedules ADD COLUMN plan_id INTEGER REFERENCES report_plans(id) ON DELETE SET NULL;
ALTER TABLE email_schedules ADD COLUMN subject TEXT NOT NULL DEFAULT '';
ALTER TABLE email_schedules ADD COLUMN on_approval INTEGER NOT NULL DEFAULT 0;

-- One month's review of one tenant. No row means Draft.
CREATE TABLE report_reviews (
    id          INTEGER PRIMARY KEY,
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    period      TEXT NOT NULL,              -- "2026-10", the schedule period it goes out in
    state       TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft', 'in_review', 'approved', 'sent')),
    reviewer    TEXT,                        -- username of the staff member assigned
    commentary  TEXT NOT NULL DEFAULT '',    -- printed under the verdict
    approved_by TEXT,
    approved_at TEXT,
    sent_at     TEXT,
    error       TEXT,                        -- the last send's error, staff only
    updated_by  TEXT,
    updated_at  TEXT NOT NULL,
    UNIQUE (tenant_id, period)
);

-- "What we recommend next", kept per tenant across months.
CREATE TABLE report_actions (
    id           INTEGER PRIMARY KEY,
    tenant_id    INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '',
    rule_key     TEXT,
    status       TEXT NOT NULL DEFAULT 'planned'
                 CHECK (status IN ('planned', 'in_progress', 'waiting', 'done')),
    target_month TEXT,                       -- "2026-11"
    position     INTEGER NOT NULL DEFAULT 0,
    done_at      TEXT,
    created_by   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX idx_report_actions_tenant ON report_actions(tenant_id, position);

-- Notes on a check (and optionally one item it found), shown on the report.
-- 'staff' notes never reach a client or a PDF.
CREATE TABLE finding_notes (
    id         INTEGER PRIMARY KEY,
    tenant_id  INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    rule_key   TEXT NOT NULL,
    subject    TEXT NOT NULL DEFAULT '',     -- the finding's subject, '' for the whole check
    body       TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility IN ('client', 'staff')),
    created_by TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_finding_notes_tenant ON finding_notes(tenant_id, rule_key);

-- Approval freezes the files: who approved them and why a later version was issued.
ALTER TABLE issued_reports ADD COLUMN approved_by TEXT;
ALTER TABLE issued_reports ADD COLUMN approved_at TEXT;
ALTER TABLE issued_reports ADD COLUMN reason TEXT;
