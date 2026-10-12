-- What a month-end reviewer left out of one client's report for one month (month_end.leave_out): a check,
-- a key figure, a recommendation, a part of the monthly review or a block of a report design. The shared
-- design and other months are untouched; the client's PDF, email and portal copy leave these out.
--
-- item is "check:<rule key>", "kpi:<key>", "action:<report_actions id>", "part:<name>",
-- "block:<design id>:<block id>" or "notes:<design id>" (that design's setup notes).
CREATE TABLE report_hidden (
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    period      TEXT NOT NULL,
    item        TEXT NOT NULL,
    created_by  TEXT,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, period, item)
);
