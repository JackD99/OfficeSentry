-- Admin-consent links. The signed state in a consent link carries a random
-- nonce; only its hash is stored here, tied to the admin and the signed-in
-- browser (sessions.token_hash) that opened the link. The callback accepts a
-- nonce once, before it expires, from that same browser.
CREATE TABLE consent_requests (
    nonce_hash   TEXT PRIMARY KEY,
    tenant_id    INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_hash TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    used_at      TEXT
);
CREATE INDEX idx_consent_requests_expires ON consent_requests(expires_at);
CREATE INDEX idx_consent_requests_tenant ON consent_requests(tenant_id);
