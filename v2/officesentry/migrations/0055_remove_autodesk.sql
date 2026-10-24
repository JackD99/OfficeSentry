-- Autodesk licences (migration 0053) were taken out before 2.6.0 was released, and 0053 with them. A database
-- upgraded while they were on main still has their two tables, their settings and any collection of theirs;
-- this removes them. On every other database it changes nothing.
DELETE FROM runs WHERE collector = 'autodesk';
DELETE FROM state_series WHERE collector = 'autodesk';
DELETE FROM state_packs WHERE kind IN ('autodesk_team', 'autodesk_subscription', 'autodesk_seat');
DELETE FROM metrics WHERE key LIKE 'autodesk.%';
DELETE FROM settings WHERE key LIKE 'autodesk.%';
DROP TABLE IF EXISTS autodesk_teams;
DROP TABLE IF EXISTS autodesk_sign_ins;
