-- Background tasks (tasks.py): long work the portal and the worker's main loop
-- hand to the worker's task threads, so no web request renders a month of
-- reports and the schedule never waits on an email. Month-end approvals and
-- scheduled report emails go through here.
--
-- A task is claimed with a lease that the worker renews while it runs. A task
-- whose lease ran out (the worker died) is queued again; ``checkpoint`` says
-- how far it got, and is saved in the same transaction as that work, so a
-- retry never repeats it. At most one task per ``key`` is queued or running
-- ("approve:<tenant>:<period>"), so a double click or two admins approve once.
CREATE TABLE tasks (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,
    key          TEXT,
    batch        TEXT,
    tenant_id    INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    payload      TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    priority     INTEGER NOT NULL DEFAULT 0,
    attempts     INTEGER NOT NULL DEFAULT 0,
    lease_until  TEXT,
    checkpoint   TEXT,
    progress     TEXT,
    result       TEXT,
    error        TEXT,
    requested_by TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);
CREATE UNIQUE INDEX idx_tasks_key ON tasks(key) WHERE status IN ('queued', 'running');
CREATE INDEX idx_tasks_status ON tasks(status, priority, created_at);
CREATE INDEX idx_tasks_batch ON tasks(batch);
CREATE INDEX idx_tasks_tenant ON tasks(tenant_id, kind);

-- Collector runs get the same lease, so a run left 'running' by a worker that
-- died is failed once its lease runs out, rather than every running run being
-- failed whenever a worker starts (which made a second worker process unsafe).
ALTER TABLE runs ADD COLUMN lease_until TEXT;
