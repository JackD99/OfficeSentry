-- Project recommendations (projects.py) are worked out from each tenant's
-- latest data whenever they're shown. This keeps what the account manager did
-- with one: proposed it to the client, agreed it, or the client declined.
-- No row means "Suggested". Reporting only: nothing here touches a tenant.
CREATE TABLE project_status (
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    project_key TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('suggested', 'proposed', 'agreed', 'declined')),
    note        TEXT NOT NULL DEFAULT '',
    updated_by  TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, project_key)
);
