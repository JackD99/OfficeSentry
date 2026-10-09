-- Activity history (events.py): sign-ins and directory audit events kept in
-- monthly SQLite files under <data dir>/events/, outside this database.
--
-- Each tenant's rows in those files carry its key from event_tenants, not
-- tenants.id. tenants.id can be handed out again after the newest tenant is
-- deleted; these keys never are (AUTOINCREMENT), so a row a failed delete left
-- behind in an event file can never show under another tenant. The worker's
-- daily prune removes rows whose key is no longer here.
CREATE TABLE event_tenants (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id  INTEGER NOT NULL UNIQUE REFERENCES tenants(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

-- How far incremental collection has read each tenant's log ('signins' or
-- 'audits'). Everything from since_at to read_to has been read except the
-- ranges in gaps (JSON [[from, to], ...], newest first, times as ISO text),
-- which the next run reads first. lost holds ranges Microsoft no longer kept
-- by the time Office Sentry got to them; pending the days whose daily
-- sign-in summary still needs rebuilding.
CREATE TABLE event_cursors (
    tenant_id  INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source     TEXT NOT NULL,
    since_at   TEXT,
    read_to    TEXT,
    gaps       TEXT NOT NULL DEFAULT '[]',
    lost       TEXT NOT NULL DEFAULT '[]',
    pending    TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, source)
) WITHOUT ROWID;
