-- Saved views a scheduled email sends (web/saved_views.py): a report's table filters, hidden columns
-- and sort, as a PDF, XLSX or CSV. A report schedule sends them for its tenant; a digest schedule
-- whose reports is ["views"] sends them across every tenant or one tenant group, to the team only.
-- name and query are a copy taken when the view is picked, kept in step when its owner saves the
-- view again (saved_view_id), so the email still goes if the saved view is deleted.
CREATE TABLE email_schedule_views (
    id            INTEGER PRIMARY KEY,
    schedule_id   INTEGER NOT NULL REFERENCES email_schedules(id) ON DELETE CASCADE,
    saved_view_id INTEGER REFERENCES saved_views(id) ON DELETE SET NULL,
    report_key    TEXT NOT NULL,
    name          TEXT NOT NULL,
    query         TEXT NOT NULL,
    format        TEXT NOT NULL CHECK (format IN ('pdf', 'xlsx', 'csv')),
    position      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_email_schedule_views ON email_schedule_views(schedule_id);
CREATE INDEX idx_email_schedule_views_saved ON email_schedule_views(saved_view_id);
