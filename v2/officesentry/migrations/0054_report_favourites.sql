-- Favourite reports (web/favourites.py): the detail reports a person starred, listed first in the All reports menu
-- and on a client's Reports tab, on every client. A person's own choice, so it goes with the person.
CREATE TABLE report_favourites (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    report_key  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, report_key)
);
