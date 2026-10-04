-- Saved table views: a person's filters, hidden columns and sort for a detail
-- report, kept as the report page's query string so they apply to any tenant.
CREATE TABLE saved_views (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    report_key  TEXT NOT NULL,
    name        TEXT NOT NULL,
    query       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (user_id, report_key, name)
);
CREATE INDEX idx_saved_views_user ON saved_views(user_id, report_key);
