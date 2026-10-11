-- Roles and capabilities (permissions.py, roles.py, web/access.py).
--
-- A role is a set of capabilities (what someone may do) and a scope (every client, or the clients and
-- groups granted to the person). Seven built-in roles are seeded by key; an admin can edit them (not
-- Administrator) and add custom ones. Every user gets the built-in role that matches what users.role
-- meant before: admin -> Administrator, analyst -> Second line, client -> Client viewer, so nobody's
-- access changes on upgrade. users.role stays as the coarse kind (admin, analyst = staff, client).
CREATE TABLE roles (
    id           INTEGER PRIMARY KEY,
    key          TEXT UNIQUE,                      -- built-in roles only; NULL for custom ones
    name         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description  TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL CHECK (kind IN ('admin', 'staff', 'client')),
    scope        TEXT NOT NULL DEFAULT 'granted' CHECK (scope IN ('all', 'granted')),
    capabilities TEXT NOT NULL DEFAULT '[]',       -- JSON list of capability keys (permissions.KEYS)
    created_at   TEXT NOT NULL,
    updated_at   TEXT
);

INSERT INTO roles (id, key, name, description, kind, scope, capabilities, created_at)
    VALUES (1, 'administrator', 'Administrator', 'Everything, on every client: settings, users and roles, the app connection.',
            'admin', 'all', '["activity_log.read", "clients.compare", "clients.manage", "collect", "designs.manage", "emails.manage", "emails.view", "export", "findings.manage", "month_end.approve", "month_end.review", "projects.manage", "projects.view", "reports.activity", "reports.changes", "reports.devices", "reports.email", "reports.files", "reports.identity", "reports.people", "reports.security", "reports.tenant", "settings.manage", "system.manage", "users.manage"]', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
INSERT INTO roles (id, key, name, description, kind, scope, capabilities, created_at)
    VALUES (2, 'team_leader', 'Team leader', 'Every client and every report; manages clients, approves month-end reports. Not users or settings.',
            'staff', 'all', '["activity_log.read", "clients.compare", "clients.manage", "collect", "designs.manage", "emails.manage", "emails.view", "export", "findings.manage", "month_end.approve", "month_end.review", "projects.manage", "projects.view", "reports.activity", "reports.changes", "reports.devices", "reports.email", "reports.files", "reports.identity", "reports.people", "reports.security", "reports.tenant"]', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
INSERT INTO roles (id, key, name, description, kind, scope, capabilities, created_at)
    VALUES (3, 'second_line', 'Second line', 'Every report on their clients; refreshes data, accepts risks, sets up emails and reviews month-end.',
            'staff', 'granted', '["clients.compare", "collect", "designs.manage", "emails.manage", "emails.view", "export", "findings.manage", "month_end.review", "projects.manage", "projects.view", "reports.activity", "reports.changes", "reports.devices", "reports.email", "reports.files", "reports.identity", "reports.people", "reports.security", "reports.tenant"]', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
INSERT INTO roles (id, key, name, description, kind, scope, capabilities, created_at)
    VALUES (4, 'first_line', 'First line', 'Looks things up on their clients: people, identity, email and device reports. Nothing to change.',
            'staff', 'granted', '["clients.compare", "emails.view", "export", "reports.changes", "reports.devices", "reports.email", "reports.identity", "reports.people"]', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
INSERT INTO roles (id, key, name, description, kind, scope, capabilities, created_at)
    VALUES (5, 'account_manager', 'Account manager', 'Licences, renewals, month-end reports and projects for their clients; approves and sends reports.',
            'staff', 'granted', '["clients.compare", "emails.manage", "emails.view", "export", "month_end.approve", "month_end.review", "projects.manage", "projects.view", "reports.changes", "reports.security", "reports.tenant"]', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
INSERT INTO roles (id, key, name, description, kind, scope, capabilities, created_at)
    VALUES (6, 'auditor', 'Auditor', 'Read-only: every report and download on their clients, nothing to change.',
            'staff', 'granted', '["clients.compare", "emails.view", "export", "projects.view", "reports.activity", "reports.changes", "reports.devices", "reports.email", "reports.files", "reports.identity", "reports.people", "reports.security", "reports.tenant"]', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
INSERT INTO roles (id, key, name, description, kind, scope, capabilities, created_at)
    VALUES (7, 'client_viewer', 'Client viewer', 'A client''s own people: their reports and downloads, their client only.',
            'client', 'granted', '["emails.view", "export", "reports.changes", "reports.devices", "reports.email", "reports.files", "reports.identity", "reports.people", "reports.security", "reports.tenant"]', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));

-- The person's role. NULL (an account made by older code, say a Microsoft sign-in that only set
-- users.role) means the built-in role for users.role, as below.
ALTER TABLE users ADD COLUMN role_id INTEGER REFERENCES roles(id) ON DELETE SET NULL;
UPDATE users SET role_id = (
    SELECT id FROM roles WHERE key = CASE users.role
        WHEN 'admin' THEN 'administrator'
        WHEN 'analyst' THEN 'second_line'
        ELSE 'client_viewer' END
);

-- Per-person changes on top of the role: one capability added (allow = 1) or taken away (allow = 0),
-- everywhere (tenant_id NULL) or on one client. Taken away wins over the role and over any allow.
CREATE TABLE user_access (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    capability TEXT NOT NULL,
    tenant_id  INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    allow      INTEGER NOT NULL CHECK (allow IN (0, 1)),
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX idx_user_access_one ON user_access(user_id, capability, COALESCE(tenant_id, 0));
CREATE INDEX idx_user_access_tenant ON user_access(tenant_id);
