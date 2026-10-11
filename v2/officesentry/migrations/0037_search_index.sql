-- What the sidebar search finds, one row per tenant, object and value (search_index.py), so a search
-- reads this table instead of scanning every tenant's latest snapshot. The worker rebuilds a tenant's
-- rows after its collection; each tenant's fingerprint (the runs it was built from) is the
-- summary_cache row of kind "search", and a tenant whose rows are behind is scanned as before.
CREATE TABLE search_index (
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL,       -- search_index.KINDS key
    run_id    INTEGER NOT NULL,    -- the run holding the object
    object_id TEXT NOT NULL,
    value     TEXT NOT NULL        -- lower case
);
CREATE INDEX idx_search_index_tenant ON search_index(tenant_id);
CREATE INDEX idx_search_index_value ON search_index(value);
