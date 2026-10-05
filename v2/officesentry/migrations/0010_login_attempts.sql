-- Password sign-in attempts, for throttling by address and by name
-- (web/throttle.py). A row is written before the password is checked
-- (outcome 'pending'), so parallel requests can't slip past the limits, and
-- then marked 'ok' or 'failed'. username is the name as typed, lower-cased:
-- unknown names are throttled the same way as real ones. 'ok' rows remember
-- the addresses a person has signed in from; the worker prunes old rows daily.
CREATE TABLE login_attempts (
    id       INTEGER PRIMARY KEY,
    ts       TEXT NOT NULL,
    ip       TEXT NOT NULL,
    username TEXT NOT NULL,
    user_id  INTEGER REFERENCES users(id) ON DELETE CASCADE,
    outcome  TEXT NOT NULL DEFAULT 'pending',
    slowed   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_login_attempts_ip ON login_attempts(ip, ts);
CREATE INDEX idx_login_attempts_name ON login_attempts(username, ts);
CREATE INDEX idx_login_attempts_user ON login_attempts(user_id, ip);
CREATE INDEX idx_login_attempts_ts ON login_attempts(ts);
