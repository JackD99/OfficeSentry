-- State history (state_history.py): each collected object stored once per version, with the runs it
-- was valid between, rather than a full copy per run. Written alongside the snapshots in `objects`
-- (which reports still read) and backfilled from the snapshots retention kept, in the background.
--
-- Only new, empty tables: the upgrade is instant and changes no existing rows.

-- One per tenant, data source and kind of object, so a version row doesn't repeat the three.
CREATE TABLE state_series (
    id        INTEGER PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    collector TEXT NOT NULL,
    kind      TEXT NOT NULL,
    UNIQUE (tenant_id, collector, kind)
);

-- The runs recorded here. A data source's runs are recorded in order (run IDs only grow), so the
-- state as of any recorded run can be rebuilt.
CREATE TABLE state_runs (
    run_id      INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    collector   TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    objects     INTEGER NOT NULL
);
CREATE INDEX idx_state_runs_series ON state_runs(tenant_id, collector, run_id);

-- The content of the versions first seen in one run, for one kind: compressed lines, like an
-- `objects` pack (store.pack_lines). A pack is deleted once no version points at it.
CREATE TABLE state_packs (
    id        INTEGER PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    run_id    INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    data      BLOB NOT NULL
);
CREATE INDEX idx_state_packs_tenant ON state_packs(tenant_id);

-- One version of one object: valid from run `from_run` until `to_run`, the first recorded run of
-- its data source without it (NULL while it's current). `hash` is of its versioned content.
CREATE TABLE state_versions (
    series_id INTEGER NOT NULL REFERENCES state_series(id) ON DELETE CASCADE,
    object_id TEXT NOT NULL,
    from_run  INTEGER NOT NULL,
    to_run    INTEGER,
    hash      BLOB NOT NULL,
    pack_id   INTEGER NOT NULL,
    PRIMARY KEY (series_id, object_id, from_run)
) WITHOUT ROWID;

-- Fields that change nearly every night (last sign-in times, mailbox sizes), kept out of the
-- versions. Per series and run, one compressed row: every object's values (`full`), or only those
-- that changed since the series' previous row, with a full row at least every few rows.
CREATE TABLE state_volatile (
    series_id INTEGER NOT NULL REFERENCES state_series(id) ON DELETE CASCADE,
    run_id    INTEGER NOT NULL,
    full      INTEGER NOT NULL,
    data      BLOB NOT NULL,
    PRIMARY KEY (series_id, run_id)
);
