-- New installs require two-step sign-in for everyone, client accounts
-- included. A database with no users yet is a new install; one already in use
-- keeps the old default (admins and analysts only), so nobody is suddenly
-- asked to enrol after an upgrade. OFFICESENTRY_REQUIRE_MFA, when set,
-- overrides this either way (config.Config.mfa_policy).
INSERT INTO settings (key, value)
SELECT 'require_mfa', 'all' WHERE NOT EXISTS (SELECT 1 FROM users);
