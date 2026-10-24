-- Autotask PSA (autotask.py): tickets in the MSP's own Autotask from Office Sentry's alerts. The connection (API
-- user, its secret and the integration code, encrypted) and the ticket defaults are settings (autotask.*).

-- Each client's company in Autotask, chosen under Settings, Autotask PSA. A client without one files its tickets in
-- the MSP's own company, or none, as the settings say.
CREATE TABLE autotask_companies (
    tenant_id INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    company_id INTEGER NOT NULL,
    company_name TEXT NOT NULL,
    mapped_by TEXT,
    mapped_at TEXT NOT NULL
);

-- The MSP's active companies in Autotask as last read, to choose from and match client names against. Names only:
-- nothing about a company's contacts, contracts or tickets.
CREATE TABLE autotask_company_list (
    company_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    company_type INTEGER
);

-- Every ticket Office Sentry created or tried to create. payload is the ticket as sent (no secret is in it), so a
-- failed one can be sent again; ticket_id is Autotask's id once it was created. alert_key is the alert's own key
-- (alerts.Message.key): while a ticket for it is open (resolved_at NULL) the same alert doesn't make another, and the
-- alert that clears it adds a note to that ticket.
CREATE TABLE autotask_tickets (
    id INTEGER PRIMARY KEY,
    tenant_id INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    event TEXT NOT NULL,
    alert_key TEXT,
    title TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    payload TEXT NOT NULL,
    ticket_id INTEGER,
    ticket_number TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_at TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    resolved_at TEXT
);
CREATE INDEX autotask_tickets_retry ON autotask_tickets (retry_at) WHERE retry_at IS NOT NULL;
CREATE INDEX autotask_tickets_tenant ON autotask_tickets (tenant_id, id);
CREATE INDEX autotask_tickets_key ON autotask_tickets (alert_key) WHERE resolved_at IS NULL;
