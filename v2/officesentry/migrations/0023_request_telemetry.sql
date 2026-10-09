-- Requests each run made to Microsoft (ratelimit.Telemetry), shown on the run
-- page. The totals stay on the run (NULL for runs from before this existed);
-- the per-endpoint rows are pruned with the run log's info lines.
ALTER TABLE runs ADD COLUMN requests INTEGER;
ALTER TABLE runs ADD COLUMN throttled INTEGER;
ALTER TABLE runs ADD COLUMN wait_seconds REAL;

CREATE TABLE run_requests (
    run_id         INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    endpoint       TEXT NOT NULL,              -- endpoints.graph_key, or "Exchange <cmdlet>"
    throttle_limit TEXT NOT NULL DEFAULT '',   -- endpoints.LIMITS name; '' for Graph's general limit
    calls          INTEGER NOT NULL,           -- HTTP requests, retries included
    throttled      INTEGER NOT NULL,           -- 429 answers
    errors         INTEGER NOT NULL,           -- 5xx answers and network failures
    retry_wait     REAL NOT NULL,              -- seconds waiting to retry
    limiter_wait   REAL NOT NULL,              -- seconds held by the shared limiter
    units          INTEGER NOT NULL,           -- resource units (SharePoint, directory)
    throttle_pct   REAL,                       -- highest x-ms-throttle-limit-percentage
    PRIMARY KEY (run_id, endpoint)
) WITHOUT ROWID;
