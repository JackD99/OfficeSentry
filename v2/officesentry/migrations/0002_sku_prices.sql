-- Monthly price per licence, entered by the MSP, for licence-waste reporting.
CREATE TABLE sku_prices (
    sku_part_number TEXT PRIMARY KEY,
    monthly_price   REAL NOT NULL CHECK (monthly_price >= 0),
    updated_at      TEXT NOT NULL
);

-- Report-wide settings (currency, stale threshold...) as key/value.
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
