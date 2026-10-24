-- A client's own margin on licence prices, in percent (licences.price_book; Settings, Licence prices). NULL
-- follows the MSP's margin (setting prices.margin); a negative one is a discount.
ALTER TABLE tenants ADD COLUMN price_margin REAL CHECK (price_margin IS NULL OR price_margin BETWEEN -90 AND 500);
