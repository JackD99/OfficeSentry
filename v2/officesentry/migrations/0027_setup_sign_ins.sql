-- The setup wizard's Microsoft sign-in (app_setup.py), one row per admin. The access token is never
-- stored: only the device code, encrypted, for the 15 minutes Microsoft allows, so that the page's next
-- check can reach any portal process. It's erased as soon as the sign-in finishes, fails or is cancelled.
-- A finished row keeps what the page shows next: the connection, the app's client ID and the tenant the
-- admin signed in to (to offer it as the first tenant).
CREATE TABLE setup_sign_ins (
    id               INTEGER PRIMARY KEY,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cloud            TEXT NOT NULL,
    tenant           TEXT NOT NULL,             -- 'organizations' or the domain or ID the admin gave
    name             TEXT NOT NULL,             -- the app registration's display name
    device_code_enc  TEXT,                      -- NULL once used, failed or expired
    user_code        TEXT NOT NULL,
    verification_uri TEXT NOT NULL,
    poll_seconds     INTEGER NOT NULL,
    expires_at       TEXT NOT NULL,
    next_poll_at     TEXT,
    status           TEXT NOT NULL CHECK (status IN ('waiting', 'working', 'done', 'failed')),
    error            TEXT,
    connection_id    INTEGER REFERENCES app_connections(id) ON DELETE SET NULL,
    client_id        TEXT,
    home_tenant      TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX idx_setup_sign_ins_user ON setup_sign_ins(user_id);
