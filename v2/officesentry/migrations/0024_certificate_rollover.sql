-- Replacing an app connection's certificate without a gap (cert_lifecycle.py).
-- During the switch a connection holds two certificates: the one collection uses
-- (cert_pem, key_enc, thumbprint, not_after) and its replacement (next_*), which the
-- admin uploads to the app registration next to the old one and tests before
-- switching. After the switch the old one's thumbprint is kept (old_*) until the
-- admin confirms it's been removed from Entra. expiry_warned is the last expiry
-- warning sent for the current certificate ('60', '30', '7' or 'expired').
ALTER TABLE app_connections ADD COLUMN next_cert_pem TEXT;
ALTER TABLE app_connections ADD COLUMN next_key_enc TEXT;
ALTER TABLE app_connections ADD COLUMN next_thumbprint TEXT;
ALTER TABLE app_connections ADD COLUMN next_not_after TEXT;
ALTER TABLE app_connections ADD COLUMN next_created_at TEXT;
ALTER TABLE app_connections ADD COLUMN next_checked_at TEXT;
ALTER TABLE app_connections ADD COLUMN old_thumbprint TEXT;
ALTER TABLE app_connections ADD COLUMN old_not_after TEXT;
ALTER TABLE app_connections ADD COLUMN switched_at TEXT;
ALTER TABLE app_connections ADD COLUMN expiry_warned TEXT;
