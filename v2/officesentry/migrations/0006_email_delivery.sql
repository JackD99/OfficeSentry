-- Scheduled email delivery: contacts, report bundles on a weekly, monthly or
-- quarterly schedule, a cross-tenant digest, new-finding alerts, and a
-- delivery history with retries. Replaces report_schedules/report_deliveries.

-- People who receive email. tenant_id NULL means the MSP's own team: they can
-- be added to any tenant's emails (as a hidden copy) and to digests and alerts.
CREATE TABLE contacts (
    id         INTEGER PRIMARY KEY,
    tenant_id  INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    name       TEXT NOT NULL DEFAULT '',
    email      TEXT NOT NULL COLLATE NOCASE,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_contacts_email ON contacts(IFNULL(tenant_id, 0), email);

-- kind 'report': one tenant's report bundle. kind 'digest': every tenant, MSP team only.
CREATE TABLE email_schedules (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('report', 'digest')),
    tenant_id     INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    reports       TEXT NOT NULL DEFAULT '[]',   -- JSON list of report keys (see delivery.CATALOG)
    include_xlsx  INTEGER NOT NULL DEFAULT 1,
    message       TEXT NOT NULL DEFAULT '',     -- optional note shown at the top of the email
    frequency     TEXT NOT NULL CHECK (frequency IN ('weekly', 'monthly', 'quarterly')),
    day_of_week   INTEGER NOT NULL DEFAULT 0 CHECK (day_of_week BETWEEN 0 AND 6),    -- 0 = Monday
    day_of_month  INTEGER NOT NULL DEFAULT 1 CHECK (day_of_month BETWEEN 1 AND 28),
    hour          INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),
    timezone      TEXT NOT NULL DEFAULT '',    -- IANA name; '' = the server's OFFICESENTRY_TIMEZONE
    enabled       INTEGER NOT NULL DEFAULT 1,
    last_period   TEXT,         -- period key ("2026-10", "2026-W41", "2026-Q4") once it is done
    attempts      INTEGER NOT NULL DEFAULT 0,  -- failed attempts for the current period
    retry_at      TEXT,
    last_status   TEXT,         -- sent | failed
    last_sent_at  TEXT,
    created_by    TEXT,
    created_at    TEXT NOT NULL,
    CHECK ((kind = 'report') = (tenant_id IS NOT NULL))
);
CREATE INDEX idx_email_schedules_tenant ON email_schedules(tenant_id);

CREATE TABLE email_schedule_recipients (
    schedule_id INTEGER NOT NULL REFERENCES email_schedules(id) ON DELETE CASCADE,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    PRIMARY KEY (schedule_id, contact_id)
);

-- Who gets an email when a collection opens findings at or above min_severity
-- (the same event that posts webhook alerts, see alerts.after_sync).
-- tenant_id NULL covers every tenant (MSP team only).
CREATE TABLE email_alerts (
    id           INTEGER PRIMARY KEY,
    tenant_id    INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    min_severity TEXT NOT NULL CHECK (min_severity IN ('critical', 'high')),
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_by   TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE email_alert_recipients (
    alert_id   INTEGER NOT NULL REFERENCES email_alerts(id) ON DELETE CASCADE,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    PRIMARY KEY (alert_id, contact_id)
);

CREATE TABLE email_deliveries (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('report', 'digest', 'alert', 'test')),
    schedule_id INTEGER REFERENCES email_schedules(id) ON DELETE SET NULL,
    alert_id    INTEGER REFERENCES email_alerts(id) ON DELETE SET NULL,
    tenant_id   INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    period      TEXT,
    subject     TEXT NOT NULL,
    recipients  TEXT NOT NULL,           -- "To" addresses, comma separated
    bcc         TEXT NOT NULL DEFAULT '',
    attachments TEXT NOT NULL DEFAULT '[]',
    payload     TEXT,                    -- alert: JSON list of finding ids, so a retry resends the same alert
    attempt     INTEGER NOT NULL DEFAULT 1,
    retry_at    TEXT,                    -- alert: when the worker tries a failed send again
    transport   TEXT,
    trigger     TEXT NOT NULL DEFAULT 'schedule' CHECK (trigger IN ('schedule', 'manual', 'retry')),
    status      TEXT NOT NULL CHECK (status IN ('sent', 'failed')),
    error       TEXT,
    sent_by     TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_email_deliveries_tenant ON email_deliveries(tenant_id, id);
CREATE INDEX idx_email_deliveries_status ON email_deliveries(status, id);

-- Carry over monthly schedules from 0003: their addresses become the tenant's contacts.
INSERT INTO email_schedules (id, kind, tenant_id, reports, frequency, day_of_month, hour, timezone, enabled,
                             last_period, created_by, created_at)
SELECT id, 'report', tenant_id, '["review"]', 'monthly', day_of_month, hour,
       '', enabled, last_period, created_by, created_at
FROM report_schedules;

WITH RECURSIVE split(schedule_id, tenant_id, addr, rest) AS (
    SELECT id, tenant_id, '', recipients || ',' FROM report_schedules
    UNION ALL
    SELECT schedule_id, tenant_id, trim(substr(rest, 1, instr(rest, ',') - 1)), substr(rest, instr(rest, ',') + 1)
    FROM split WHERE rest <> ''
)
INSERT OR IGNORE INTO contacts (tenant_id, email, created_at)
SELECT DISTINCT tenant_id, addr, strftime('%Y-%m-%dT%H:%M:%SZ', 'now') FROM split WHERE addr <> '';

WITH RECURSIVE split(schedule_id, tenant_id, addr, rest) AS (
    SELECT id, tenant_id, '', recipients || ',' FROM report_schedules
    UNION ALL
    SELECT schedule_id, tenant_id, trim(substr(rest, 1, instr(rest, ',') - 1)), substr(rest, instr(rest, ',') + 1)
    FROM split WHERE rest <> ''
)
INSERT OR IGNORE INTO email_schedule_recipients (schedule_id, contact_id)
SELECT s.schedule_id, c.id FROM split s JOIN contacts c ON c.tenant_id = s.tenant_id AND c.email = s.addr
WHERE s.addr <> '';

INSERT INTO email_deliveries (kind, schedule_id, tenant_id, subject, recipients, status, error, created_at)
SELECT 'report', schedule_id, tenant_id, 'Monthly Security & Licensing Review', recipients, status, error, sent_at
FROM report_deliveries;

DROP TABLE report_deliveries;
DROP TABLE report_schedules;
