-- Collection performance.
--
-- The "latest run of each collector" lookups (snapshots, freshness, the
-- schedule) read runs by tenant, collector and status; without this index
-- they sorted every run the tenant ever had. The slot index serves the
-- nightly schedule's "already queued for this slot?" check.
CREATE INDEX idx_runs_latest ON runs(tenant_id, collector, status, finished_at, id);
CREATE INDEX idx_runs_slot ON runs(tenant_id, collector, scheduled_for);
-- A tenant's most recent run, which says whether its dashboard row is current.
CREATE INDEX idx_runs_finished ON runs(tenant_id, finished_at, id);

-- Higher runs first: "Run now" and refreshes before an emailed report (1)
-- don't wait behind the nightly backlog (0).
ALTER TABLE runs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;
UPDATE runs SET priority = 1 WHERE status = 'queued' AND trigger = 'manual';

-- One row per tenant for the dashboard and the All tenants review, so they
-- don't rebuild every tenant's Monthly Review on each page load.
-- ``fingerprint`` says what the row was built from (latest run, findings,
-- prices, the day); a row whose fingerprint no longer matches is rebuilt.
CREATE TABLE rollup_cache (
    tenant_id   INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    data        TEXT NOT NULL,
    built_at    TEXT NOT NULL
);

ANALYZE;
