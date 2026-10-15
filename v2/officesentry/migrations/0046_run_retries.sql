-- A collection that failed for a reason that passes by waiting (Microsoft busy or out of reach, the worker
-- restarted mid-run) is tried again by itself (store.plan_retry, store.queue_retries). retry_at is set on
-- the failed run: when it's tried again. A retry's retry_of is the first run of the ones it tries again
-- for, so the tries of one failure are counted and an alert waits for the last of them.
ALTER TABLE runs ADD COLUMN retry_at TEXT;
ALTER TABLE runs ADD COLUMN retry_of INTEGER;
CREATE INDEX idx_runs_retry_at ON runs(retry_at) WHERE retry_at IS NOT NULL;
CREATE INDEX idx_runs_retry_of ON runs(retry_of) WHERE retry_of IS NOT NULL;
