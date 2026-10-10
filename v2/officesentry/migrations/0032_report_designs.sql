-- Report designs (reports/designs.py): client reports an account manager builds from blocks in the
-- portal, without code. ``blocks`` is a JSON list of {"kind", "width", "settings"} in page order and
-- ``settings`` the document's own options (cover title and subtitle). A design is rendered for any
-- tenant from stored data, as a page and a PDF, and a report plan or schedule can send it as
-- "design:<id>".
CREATE TABLE report_designs (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    settings    TEXT NOT NULL DEFAULT '{}',
    blocks      TEXT NOT NULL DEFAULT '[]',
    created_by  TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT
);
