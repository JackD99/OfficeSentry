-- Wrong passwords typed in one signed-in browser to confirm a security change (auth.confirm_password). The
-- fifth signs that browser out. They used to count towards the account's two-step lock, which let someone
-- holding a stolen session lock its owner out of signing in.
ALTER TABLE sessions ADD COLUMN wrong_passwords INTEGER NOT NULL DEFAULT 0;
