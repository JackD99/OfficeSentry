-- Acting on a finding (findings.py, web/finding.py): what the team does with one, kept in Office Sentry's own
-- database. Nothing here is ever written to a client's tenant, and none of it reaches a client's reports,
-- PDFs, emails or pages.
-- A staff note on the finding (the latest replaces it; empty clears it).
ALTER TABLE findings ADD COLUMN note TEXT;
ALTER TABLE findings ADD COLUMN note_by TEXT;
ALTER TABLE findings ADD COLUMN note_at TEXT;
-- Who on the team holds it. Deleting the account hands it back; a finding that comes back is handed to nobody.
ALTER TABLE findings ADD COLUMN handed_to INTEGER REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE findings ADD COLUMN handed_by TEXT;
ALTER TABLE findings ADD COLUMN handed_at TEXT;
-- Out of Today's list until then (NULL or past: not snoozed). Cleared when the finding resolves or comes back.
ALTER TABLE findings ADD COLUMN snoozed_until TEXT;
ALTER TABLE findings ADD COLUMN snoozed_by TEXT;
ALTER TABLE findings ADD COLUMN snoozed_at TEXT;
