-- Tenant groups (tenant_groups.py): the MSP's own way to slice its clients, by
-- tier, region, service level or anything else. A tenant can be in any number
-- of groups, or none.
--
-- Groups never widen what anyone sees on their own. They filter staff pages
-- (dashboard, All tenants, What changed, month-end queue, Client tenants) over
-- the tenants someone already sees, and scope a digest. The one way a group
-- grants access is an admin giving an analyst the group itself
-- (user_tenant_groups): that analyst then sees every tenant in it, including
-- ones added later. access.visible_tenants() is the only place that reads it,
-- and it ignores group grants for any role but analyst.
CREATE TABLE tenant_groups (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    category    TEXT NOT NULL DEFAULT '',  -- optional heading in pickers: "Tier", "Region", "Service level"
    description TEXT NOT NULL DEFAULT '',
    created_by  TEXT,
    created_at  TEXT NOT NULL
);

-- Small tables (an MSP has tens of groups): the primary keys are the only indexes they need.
CREATE TABLE tenant_group_members (
    group_id  INTEGER NOT NULL REFERENCES tenant_groups(id) ON DELETE CASCADE,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, tenant_id)
) WITHOUT ROWID;

-- An analyst granted a group sees every tenant in it (on top of user_tenants).
CREATE TABLE user_tenant_groups (
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    group_id INTEGER NOT NULL REFERENCES tenant_groups(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, group_id)
) WITHOUT ROWID;

-- The group a signed-in person chose to look at; NULL is every tenant they see.
-- Per sign-in, so a new session starts on all tenants.
ALTER TABLE sessions ADD COLUMN group_id INTEGER REFERENCES tenant_groups(id) ON DELETE SET NULL;

-- A digest can cover one group instead of every tenant. Deleting the group
-- makes it cover every tenant again (the delete page says so).
ALTER TABLE email_schedules ADD COLUMN group_id INTEGER REFERENCES tenant_groups(id) ON DELETE SET NULL;
