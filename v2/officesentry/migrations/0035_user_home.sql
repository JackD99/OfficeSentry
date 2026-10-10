-- Each staff member's home page (home.py): 'today' (what needs them today) or 'clients' (licence waste,
-- renewals and reports to approve). Clients keep their own home and never read it.
ALTER TABLE users ADD COLUMN home TEXT NOT NULL DEFAULT 'today' CHECK (home IN ('today', 'clients'));
