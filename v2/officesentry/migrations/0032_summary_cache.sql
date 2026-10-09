-- Precomputed cross-tenant rows (reports/precomputed.py): each tenant's line on What changed
-- (kind "changes:7", "changes:30", "changes:90") and on the check matrix ("checks"), so those
-- pages read one stored row per tenant instead of building every tenant's reports.
-- ``fingerprint`` says what the row was built from; a row whose fingerprint no longer matches
-- is built again.
CREATE TABLE summary_cache (
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    data        TEXT NOT NULL,
    built_at    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, kind)
);
