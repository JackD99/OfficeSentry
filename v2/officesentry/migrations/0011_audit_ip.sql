-- The address an audited action came from (the portal's request.remote_addr,
-- which honours OFFICESENTRY_TRUSTED_PROXIES). Empty for the CLI and worker,
-- and for rows written before this migration. Indexes for the Activity log
-- page's filters.
ALTER TABLE audit_log ADD COLUMN ip TEXT;
CREATE INDEX idx_audit_action ON audit_log(action, ts);
CREATE INDEX idx_audit_tenant ON audit_log(tenant_id, ts);
CREATE INDEX idx_audit_username ON audit_log(username, ts);
