-- Passwords an admin chose (a new user, or a reset) have to be replaced at the
-- next sign-in: before setting up two-step for accounts without it, and right
-- after the two-step code for accounts with it. Existing accounts aren't affected.
ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0;
