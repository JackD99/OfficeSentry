-- Autodesk licences (autodesk/): each client's Autodesk seats and who holds them, read from Autodesk's Business
-- Success Plan Reporting API. The app the MSP registered with Autodesk (its client ID and secret) is kept in
-- settings (autodesk.client_id, autodesk.client_secret_enc).
--
-- An Autodesk sign-in is a personal access token an admin of one or more Autodesk teams made for that app.
-- One token sees every team its person administers, which may be several clients' teams, so it belongs to no
-- client: teams says what it saw at its last check (JSON [{"id", "name", "benefits"}]), for the admin page only.
CREATE TABLE autodesk_sign_ins (
    id          INTEGER PRIMARY KEY,
    label       TEXT NOT NULL,
    pat_enc     TEXT NOT NULL,
    added_by    TEXT,
    added_at    TEXT NOT NULL,
    checked_at  TEXT,
    check_error TEXT,
    teams       TEXT
);

-- Which Autodesk team belongs to which client, picked by an admin on the client's Settings tab. A team belongs
-- to one client at most (the primary key), and a client's collection only ever asks Autodesk for its own teams,
-- so a sign-in that sees several clients never mixes them. sign_in_id is the sign-in that reads it.
CREATE TABLE autodesk_teams (
    team_id     TEXT PRIMARY KEY,
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    sign_in_id  INTEGER REFERENCES autodesk_sign_ins(id) ON DELETE SET NULL,
    name        TEXT,
    added_by    TEXT,
    added_at    TEXT NOT NULL
);
CREATE INDEX idx_autodesk_teams_tenant ON autodesk_teams (tenant_id);
