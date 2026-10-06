-- Microsoft 365 national clouds (clouds.py). A tenant lives in one cloud, and an
-- app registration only works in the cloud it was registered in, so both record it.
-- 'commercial' (which includes GCC) keeps every existing tenant and app as it was.
ALTER TABLE tenants ADD COLUMN cloud TEXT NOT NULL DEFAULT 'commercial'
    CHECK (cloud IN ('commercial', 'gcc_high', 'dod', 'china'));
ALTER TABLE app_connections ADD COLUMN cloud TEXT NOT NULL DEFAULT 'commercial'
    CHECK (cloud IN ('commercial', 'gcc_high', 'dod', 'china'));
