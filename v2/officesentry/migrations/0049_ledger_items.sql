-- Morning ledger (home.py, web/home.py): what the team does with a line on Today that isn't a finding (a
-- client's failed collection, a client waiting for access, something due), kept in Office Sentry's own
-- database and never written to a client's tenant. Findings keep theirs on their own row (migration 0048).
-- item_key is the line's key: collection, access, due:<kind> or due:<kind>:<title>. A row goes when its
-- line has cleared, so a later failure starts with nobody holding it.
CREATE TABLE item_state (
    id INTEGER PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    item_key TEXT NOT NULL,
    handed_to INTEGER REFERENCES users(id) ON DELETE SET NULL,
    handed_by TEXT,
    handed_at TEXT,
    snoozed_until TEXT,
    snoozed_by TEXT,
    snoozed_at TEXT,
    UNIQUE (tenant_id, item_key)
);
-- "Since you last looked": when this person last opened Today, and where its line was then. Opening it again
-- within a couple of hours keeps the line; coming back later moves it to the last visit. Never written for
-- the public demo's visitor.
ALTER TABLE users ADD COLUMN today_opened_at TEXT;
ALTER TABLE users ADD COLUMN today_since TEXT;
