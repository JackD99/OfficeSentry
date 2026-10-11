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
-- A wizard sign-in that updates an existing app's permissions is a setup_sign_ins row with purpose
-- 'update' (the column is from 0038), its connection_id set from the start.
ALTER TABLE app_connections ADD COLUMN permissions_release TEXT;

CREATE TABLE permission_requests (
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    release     TEXT NOT NULL,
    sent_at     TEXT,
    sent_by     TEXT,
    approved_at TEXT,
    PRIMARY KEY (tenant_id, release)
);
