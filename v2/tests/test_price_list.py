"""Importing a reseller's price list: reading whatever they export, matching it to licences, saving it."""

from officesentry import price_list
from tests.conftest import login, make_user
from tests.seed import seed_tenant

RESELLER = (
    "Product name,SKU,Unit price (GBP),List price (GBP)\r\n"
    "Microsoft 365 Business Premium (NCE Annual commitment),SPB,18.10,20.60\r\n"
    "Exchange Online (Plan 1),EXCHANGESTANDARD,3.30,3.80\r\n"
    "Azure Active Directory Premium P1,,5.20,6.00\r\n"
    "Dynamics 365 Sales Enterprise,DYN365_SALES,78.40,88.10\r\n"
    "Microsoft 365 Business Premium (monthly term),SPB,21.70,24.70\r\n"
    "Teams Phone Standard,MCOEV,,8.90\r\n"
)


def _parsed(db, text=RESELLER, **kwargs):
    return price_list.parse(db, text, **kwargs)


def test_it_reads_a_resellers_export(db):
    parsed = _parsed(db)
    assert parsed.error is None
    assert parsed.columns == ("SKU and Product name", "Unit price (GBP)")  # what you pay, not the list price
    assert parsed.currency == "GBP"
    prices = {r.part: r.price for r in parsed.matched}
    assert prices == {"SPB": 18.10, "EXCHANGESTANDARD": 3.30, "AAD_PREMIUM": 5.20}
    how = {r.part: r.how for r in parsed.matched}
    assert how == {"SPB": "part number", "EXCHANGESTANDARD": "part number", "AAD_PREMIUM": "name"}
    left_out = {r.text: r.problem for r in parsed.unmatched}
    assert left_out == {
        "Dynamics 365 Sales Enterprise": "Not a licence Office Sentry reports on",  # the row as the file reads
        "Microsoft 365 Business Premium (monthly term)": "The same licence as row 2",
        "Teams Phone Standard": "No price",
    }


def test_names_are_matched_through_the_wording_resellers_add(db):
    rows = {
        "Microsoft 365 Business Premium": "SPB",
        "MICROSOFT 365 BUSINESS PREMIUM (NCE, ANNUAL)": "SPB",
        "365 Business Premium - 1 user, per month": "SPB",
        "Microsoft Office 365 E3": "ENTERPRISEPACK",
        "Office 365 Business Premium": "O365_BUSINESS_PREMIUM",  # Microsoft's old name for Business Standard
        "Azure AD Premium P2 subscription licence": "AAD_PREMIUM_P2",
        "Copilot for Microsoft 365": "Microsoft_365_Copilot",
        "Exchange Online (Plan 2)": "EXCHANGEENTERPRISE",
        "Microsoft 365 Business Premium (no Teams)": "Microsoft_365_ Business_ Premium_(no Teams)",
    }
    for text, part in rows.items():
        assert price_list.part_for(text) == (part, "name"), text
    assert price_list.part_for("Microsoft 365 E3 EEA (no Teams)") == (None, "")  # not a licence we name
    assert price_list.part_for("spb") == ("SPB", "part number")  # a part number however it's written
    assert price_list.part_for("Seats") == (None, "")


def test_prices_are_read_however_theyre_written():
    assert price_list.money("£18.10") == 18.10
    assert price_list.money(" 7.00 USD ") == 7.00
    assert price_list.money("1,234.56") == 1234.56
    assert price_list.money("18,10") == 18.10  # a decimal comma
    assert price_list.money("1.234,56") == 1234.56
    assert price_list.money("$0") == 0
    for nothing in ("", "-", "Included", "n/a", "POA"):
        assert price_list.money(nothing) is None


def test_a_years_prices_are_divided_by_twelve(db):
    text = "Product,Annual price\r\nMicrosoft 365 Business Premium,217.20\r\n"
    parsed = _parsed(db, text)
    assert parsed.yearly and parsed.matched[0].price == 18.10
    assert _parsed(db, text, yearly=False).matched[0].price == 217.20  # the page can say otherwise
    monthly = "Product,Price\r\nMicrosoft 365 Business Premium,18.10\r\n"
    assert not _parsed(db, monthly).yearly
    assert _parsed(db, monthly, yearly=True).matched[0].price == 1.51


def test_tabs_and_semicolons_read_as_well(db):
    tabs = "Offer\tCost\r\nMicrosoft 365 E3\t36.60\r\n"
    semis = "Description;Your price;Qty\r\nMicrosoft 365 E5;55.30;12\r\n"
    assert _parsed(db, tabs).matched[0].part == "SPE_E3"
    assert _parsed(db, semis).matched[0].price == 55.30


def test_a_list_without_the_columns_says_so(db):
    parsed = _parsed(db, "Licence,Seats\r\nMicrosoft 365 Business Premium,12\r\n")
    assert "couldn't find both a licence column and a price column" in parsed.error
    assert "Licence, Seats" in parsed.error  # the headings it saw, so they can see what it read
    assert _parsed(db, "").error and _parsed(db, "Product,Price\r\n").error
    assert "over 400 kB" in _parsed(db, "Product,Price\r\n" + "Microsoft 365 E3,9.99\r\n" * 20_000).error


def test_the_page_previews_a_list_then_saves_it(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    assert "Check the list" in client.get("/admin/prices/import").get_data(as_text=True)
    page = client.post("/admin/prices/import", data={"text": RESELLER}).get_data(as_text=True)
    assert "Microsoft 365 Business Premium" in page and "£18.10" in page
    assert "Not a licence Office Sentry reports on" in page and "The same licence as row 2" in page
    assert "Read <strong>SKU and Product name</strong>" in page
    resp = client.post(
        "/admin/prices/import",
        data={"text": RESELLER, "asked": "1", "yearly": "0", "confirm": "1"},
        follow_redirects=True,
    )
    assert "Saved 3 prices from your price list." in resp.get_data(as_text=True)
    held = {r["sku_part_number"]: r["monthly_price"] for r in db.execute("SELECT * FROM sku_prices")}
    assert held == {"SPB": 18.10, "EXCHANGESTANDARD": 3.30, "AAD_PREMIUM": 5.20}
    assert [r["action"] for r in db.execute("SELECT action FROM audit_log")].count("prices_imported") == 1
    # Importing the same list again changes nothing, and says so rather than saving it twice.
    page = client.post("/admin/prices/import", data={"text": RESELLER}).get_data(as_text=True)
    assert "No change" in page


def test_only_people_who_manage_settings_can_import(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "someone", role="client", tenants=(alpha,))
    login(client, "someone")
    assert client.get("/admin/prices/import").status_code == 403
    assert client.post("/admin/prices/import", data={"text": RESELLER}).status_code == 403
