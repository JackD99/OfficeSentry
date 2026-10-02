-- Prospect assessments: public-information checks on a domain that isn't a client yet.
CREATE TABLE prospect_assessments (
    id          INTEGER PRIMARY KEY,
    domain      TEXT NOT NULL,
    company     TEXT,
    data        TEXT NOT NULL,              -- JSON: tenant lookup and domain health
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_prospects_domain ON prospect_assessments(domain, created_at);
