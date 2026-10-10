-- Onboarding many tenants (onboarding.py, pages in web/onboarding.py): who to send each new
-- client's consent pack to, and how far each one has got. One row per tenant that was imported
-- or had its consent link sent; a tenant without a row is simply "not sent".
--
-- Nothing here is proof of consent. approved_at only says someone came back from Microsoft's
-- consent page through the link sent to the client; Office Sentry then signs in to the tenant
-- (a consent_check task) and only that sets tenants.consented_at. checked_at and check_error
-- are the last such sign-in and, while it fails, why, in Office Sentry's own words.
CREATE TABLE onboarding (
    tenant_id     INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    contact_name  TEXT NOT NULL DEFAULT '',
    contact_email TEXT NOT NULL DEFAULT '',
    imported_at   TEXT,
    imported_by   TEXT,
    sent_at       TEXT,
    sent_by       TEXT,
    approved_at   TEXT,
    checked_at    TEXT,
    check_error   TEXT
);
