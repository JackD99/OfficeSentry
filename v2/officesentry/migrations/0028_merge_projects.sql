-- Projects are now half a day's work or more (projects.MIN_HOURS), so two small
-- ones were folded into bigger ones: Admin account hardening into Identity and
-- Conditional Access, and Teams and groups governance into Sharing, guests and
-- Teams governance. Keep what account managers recorded: the bigger project's
-- own status wins, and the old project's note is added to its note.
UPDATE project_status AS merged SET note = TRIM(merged.note || ' ' || old.note)
  FROM project_status AS old
  WHERE old.tenant_id = merged.tenant_id AND old.note <> ''
    AND ((old.project_key = 'admin_hardening' AND merged.project_key = 'identity')
      OR (old.project_key = 'teams_governance' AND merged.project_key = 'external_sharing'));
INSERT OR IGNORE INTO project_status (tenant_id, project_key, status, note, updated_by, updated_at)
  SELECT tenant_id, CASE project_key WHEN 'admin_hardening' THEN 'identity' ELSE 'external_sharing' END,
         status, note, updated_by, updated_at
  FROM project_status WHERE project_key IN ('admin_hardening', 'teams_governance');
DELETE FROM project_status WHERE project_key IN ('admin_hardening', 'teams_governance');
