-- Client tenant lifecycle: pause, archive and notes.
--
-- tenants.enabled (from 0001) is the pause switch: 0 stops scheduled
-- collection, emailed reports and alerts for the tenant, and keeps its data.
-- paused_at says since when. An archived tenant is also paused, and is hidden
-- from tenant lists, dashboards, rollups and clients until it is restored.
ALTER TABLE tenants ADD COLUMN paused_at TEXT;
ALTER TABLE tenants ADD COLUMN archived_at TEXT;
ALTER TABLE tenants ADD COLUMN notes TEXT NOT NULL DEFAULT '';

UPDATE tenants SET paused_at = created_at WHERE enabled = 0;
