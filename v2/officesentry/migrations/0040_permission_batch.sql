-- Permission batches (permission_batch.py): new read-only permissions released together, which each
-- client approves once with the consent link it was sent before.
--
-- app_connections.permissions_release is the batch the MSP's app registration was last given (by the
-- setup wizard, `create-app`, the update wizard or `update-app`, or an admin saying they added them by
-- hand); NULL means the permissions Office Sentry asked for before its first batch.
--
-- permission_requests tracks one batch for one client: when the approval link went out, and when the
-- client's admin came back from Microsoft's page through it. Neither is proof: the connection check
-- reads which permissions the tenant actually granted.
--
-- setup_sign_ins.purpose says what a wizard sign-in is for: creating the app, or updating the
-- permissions of an existing one (connection_id names it from the start).
ALTER TABLE app_connections ADD COLUMN permissions_release TEXT;

ALTER TABLE setup_sign_ins ADD COLUMN purpose TEXT NOT NULL DEFAULT 'create';

CREATE TABLE permission_requests (
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    release     TEXT NOT NULL,
    sent_at     TEXT,
    sent_by     TEXT,
    approved_at TEXT,
    PRIMARY KEY (tenant_id, release)
);
