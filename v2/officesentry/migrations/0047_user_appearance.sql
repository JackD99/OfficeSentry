-- How the portal looks to each person (Account, Appearance; web/appearance.py): light, dark or whatever their
-- device is set to, and, for staff, one of the colour themes in static/themes.css. Only the person's own
-- screen changes: client reports, PDFs and emails keep the MSP's branding.
ALTER TABLE users ADD COLUMN theme TEXT NOT NULL DEFAULT 'system' CHECK (theme IN ('system', 'light', 'dark'));
ALTER TABLE users ADD COLUMN accent TEXT NOT NULL DEFAULT 'green';
