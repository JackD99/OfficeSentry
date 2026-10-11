-- Client sign-in with Microsoft, and confirming a change with Microsoft (sso.py).
--
-- The tenant (Directory ID) a linked Microsoft account belongs to. A client's account is only ever linked to
-- an account in a tenant it's granted, and signs in again only from that tenant. NULL for staff accounts
-- linked before this (their tenant is the MSP's own, sso_tenant).
ALTER TABLE users ADD COLUMN entra_tid TEXT;

-- When a client's own administrator approved the sign-in app for their organisation (the approval link's
-- answer), or a person from that client first signed in with Microsoft.
ALTER TABLE tenants ADD COLUMN sign_in_approved_at TEXT;

-- When the person last confirmed it's them with Microsoft (accounts with no password confirm a security
-- change that way instead of typing a password).
ALTER TABLE sessions ADD COLUMN confirmed_at TEXT;
