-- Two-step sign-in (TOTP) and server-side sessions.

-- The authenticator secret, encrypted with the vault. totp_pending_enc holds a
-- secret that is being set up and hasn't been confirmed with a code yet.
ALTER TABLE users ADD COLUMN totp_secret_enc TEXT;
ALTER TABLE users ADD COLUMN totp_pending_enc TEXT;
ALTER TABLE users ADD COLUMN totp_enabled_at TEXT;
-- Last accepted time step, so a code can't be used twice.
ALTER TABLE users ADD COLUMN totp_last_step INTEGER;

-- One-time recovery codes (SHA-256 of the normalised code).
CREATE TABLE user_recovery_codes (
    id        INTEGER PRIMARY KEY,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    used_at   TEXT
);
CREATE INDEX idx_recovery_codes_user ON user_recovery_codes(user_id);

-- Every signed-in browser. The cookie holds a random token; only its hash is
-- stored, and deleting the row signs that browser out immediately.
CREATE TABLE sessions (
    id              INTEGER PRIMARY KEY,
    token_hash      TEXT NOT NULL UNIQUE,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_version INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    ip              TEXT,
    user_agent      TEXT
);
CREATE INDEX idx_sessions_user ON sessions(user_id);
