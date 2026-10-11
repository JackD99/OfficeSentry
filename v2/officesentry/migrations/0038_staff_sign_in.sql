-- Staff sign-in with their own Microsoft account (sso.py).
--
-- A user connected to a Microsoft account carries its Entra object ID and sign-in name. An account that only
-- signs in with Microsoft has the password hash "!" (sso.NO_PASSWORD), which never matches.
ALTER TABLE users ADD COLUMN entra_oid TEXT;
ALTER TABLE users ADD COLUMN entra_upn TEXT;
ALTER TABLE users ADD COLUMN entra_linked_at TEXT;
CREATE UNIQUE INDEX idx_users_entra_oid ON users(entra_oid) WHERE entra_oid IS NOT NULL;

-- How a session was started: 'password' or 'microsoft'. Turning passwords off for staff needs a Microsoft session.
ALTER TABLE sessions ADD COLUMN method TEXT NOT NULL DEFAULT 'password';

-- The setup wizard's device sign-in (app_setup.py) also creates the sign-in app: what a row is for.
ALTER TABLE setup_sign_ins ADD COLUMN purpose TEXT NOT NULL DEFAULT 'connection';
