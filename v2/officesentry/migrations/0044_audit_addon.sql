-- The audit log add-on (auditlog/): a second app registration that reads each opted-in client's unified audit
-- log through Microsoft's Office 365 Management Activity feed.
--
-- kind says what an app connection is for: 'main' is the shared read-only app every client approves, 'audit'
-- the add-on, which holds only ActivityFeed.Read and is approved per client. Tenants only ever use a main
-- connection (tenants.connection_id); a client's add-on is its audit_feeds row. home_tenant is the MSP's own
-- tenant the app was created in, which the feed takes as its PublisherIdentifier.
ALTER TABLE app_connections ADD COLUMN kind TEXT NOT NULL DEFAULT 'main' CHECK (kind IN ('main', 'audit'));
ALTER TABLE app_connections ADD COLUMN home_tenant TEXT;

-- One row per client the add-on was asked for. Nothing here is proof of consent: approved_at only says someone
-- came back from Microsoft's consent page through the add-on link; consented_at is set by signing in as the
-- add-on app and reading the client's feed subscriptions (auditlog/addon.confirm), as for the main app.
--
-- wanted_at/by: an admin asked for the add-on on this client (sending the request, or switching it on again),
-- so the feed is started once the client has approved. started_at/by: Office Sentry started the feed (the two
-- POST calls, auditlog/feed.py). stopped_at/by and stop_reason: switched off by an admin, by deleting the
-- client, or because the client removed the app. content_types: what Microsoft said was on at the last check.
CREATE TABLE audit_feeds (
    tenant_id      INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    connection_id  INTEGER REFERENCES app_connections(id) ON DELETE SET NULL,
    sent_at        TEXT,
    sent_by        TEXT,
    wanted_at      TEXT,
    wanted_by      TEXT,
    approved_at    TEXT,
    consented_at   TEXT,
    checked_at     TEXT,
    check_error    TEXT,
    started_at     TEXT,
    started_by     TEXT,
    content_types  TEXT,
    stopped_at     TEXT,
    stopped_by     TEXT,
    stop_reason    TEXT,
    created_at     TEXT NOT NULL
);
