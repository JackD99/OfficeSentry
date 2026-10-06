-- Technical lead overrides of the insurance evidence pack's suggested
-- questionnaire answers: one per tenant and question. The pack, its PDF and
-- exports show the override (who set it and when); `suggested` is what Office
-- Sentry suggested at the time, so the page can say when the data has changed
-- since. Reporting only: nothing here touches a tenant.
CREATE TABLE evidence_answer_overrides (
    tenant_id    INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    question_key TEXT NOT NULL,                -- evidence_answers.QUESTIONS key
    answer       TEXT NOT NULL CHECK (answer IN ('Yes', 'Partly', 'No', 'Not known')),
    note         TEXT NOT NULL DEFAULT '',
    suggested    TEXT NOT NULL CHECK (suggested IN ('Yes', 'Partly', 'No', 'Not known')),
    set_by       TEXT NOT NULL,                -- username
    set_at       TEXT NOT NULL,
    PRIMARY KEY (tenant_id, question_key)
);
