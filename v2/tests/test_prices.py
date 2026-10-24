"""Licence prices: the MSP's own, Microsoft's list prices, the MSP's margin and a client's own margin."""

from datetime import date

from officesentry import licences, list_prices, store
from officesentry.licences import SKU_NAMES, PriceBook, price_book
from officesentry.reports import data_reports
from officesentry.reports.monthly_review import build
from tests.conftest import login, make_user
from tests.seed import seed_tenant


def _settings(db, **values):
    for key, value in values.items():
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


def test_your_price_wins_then_the_list_price_then_nothing():
    book = PriceBook({"SPB": 18.10}, "USD", 0)
    assert (book.get("SPB").monthly, book.get("SPB").source) == (18.10, "yours")
    assert (book.get("SPE_E3").monthly, book.get("SPE_E3").source) == (39.00, "list")
    assert book.get("SOMETHING_ELSE") is None
    # List prices are US dollars: in any other currency only the MSP's own prices count.
    gbp = PriceBook({"SPB": 18.10}, "GBP", 0)
    assert gbp.get("SPE_E3") is None and gbp.get("SPB").monthly == 18.10
    assert PriceBook({}, "USD", 0, use_list=False).get("SPE_E3") is None


def test_the_margin_is_added_to_every_price_and_a_negative_one_is_a_discount():
    up = PriceBook({"SPB": 18.10}, "USD", 12)
    assert up.get("SPB").monthly == 20.27  # 18.10 + 12%
    assert up.get("SPE_E3").monthly == 43.68  # list 39.00 + 12%
    assert up.get("SPB").explained == "Your price + 12%"
    assert up.get("SPE_E3").explained == "List price + 12%"
    down = PriceBook({"SPB": 20.00}, "USD", -5)
    assert down.get("SPB").monthly == 19.00 and down.get("SPB").explained == "Your price − 5%"
    assert PriceBook({"SPB": 20.00}, "USD", 0).get("SPB").explained == "Your price"
    assert up.monthly()["SPB"] == 20.27 and "SPE_E5" in up.monthly()


def test_margins_are_read_with_care():
    assert licences.parse_margin("12") == 12
    assert licences.parse_margin(" 12.5% ") == 12.5
    assert licences.parse_margin("-5") == -5
    for bad in ("", "lots", "-95", "600", None):
        assert licences.parse_margin(bad) is None


def test_a_client_margin_replaces_the_msps(db, two_tenants):
    alpha, bravo = two_tenants
    _settings(db, currency="USD", **{"prices.margin": "10"})
    db.execute("UPDATE tenants SET price_margin = -5 WHERE id = ?", (alpha,))
    assert price_book(db, store.get_tenant(db, alpha)).margin == -5
    assert price_book(db, store.get_tenant(db, bravo)).margin == 10
    assert price_book(db).margin == 10  # the MSP's own, without a client
    assert licences.load_prices(db, store.get_tenant(db, alpha))["SPB"] == 20.90  # 22.00 − 5%
    assert licences.load_prices(db, store.get_tenant(db, bravo))["SPB"] == 24.20  # 22.00 + 10%


def test_the_note_says_where_prices_came_from():
    book = PriceBook({"SPB": 18.10}, "USD", 12)
    note = book.note(["SPB", "SPE_E3", "ENTERPRISEPACK", "UNKNOWN", "SPB"])
    assert note == (
        "Prices: Microsoft's list price (1 Jul 2026) for 2 licences and your own for 1 licence, plus a 12% margin."
    )
    assert PriceBook({"SPB": 1}, "GBP", -5).note(["SPB"]) == "Prices: your own for 1 licence, less a 5% discount."
    assert PriceBook({}, "GBP", 0).note(["SPB"]) == ""


def test_list_prices_are_known_licences_and_dated():
    assert list_prices.AS_OF <= date.today()
    for part, price in list_prices.PRICES.items():
        assert part in SKU_NAMES, f"{part} has a list price but no name"
        assert 0 < price < 200
    assert list_prices.price("SPB", "USD") == 22.00 and list_prices.price("SPB", "GBP") is None


def test_waste_is_in_the_clients_price(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    _settings(db, currency="USD", **{"prices.margin": "10"})
    db.execute("UPDATE tenants SET price_margin = 50 WHERE id = ?", (alpha,))
    report = build(db, store.get_tenant(db, alpha))
    assert report.licences.monthly_waste == 33.00 * (3 + 1)  # list 22.00 + 50%, 3 unassigned + Bob (disabled)


def test_the_prices_page_saves_margins(client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/admin/prices").get_data(as_text=True)
    assert "Give a client its own margin" in page and "None yet: every client uses your margin." in page
    client.post(
        "/admin/prices",
        data={"currency": "USD", "use_list": "1", "margin": "12", "new_client": str(alpha), "new_margin": "-5"},
    )
    assert licences.msp_margin(db) == 12
    assert store.get_tenant(db, alpha)["price_margin"] == -5
    assert store.get_tenant(db, bravo)["price_margin"] is None
    page = client.get("/admin/prices").get_data(as_text=True)
    assert f'name="margin:{alpha}"' in page and "1 client has its own margin" in page
    assert "$24.64" in page and "List price + 12%" in page  # Business Premium: 22.00 + 12%
    # Clearing a client's margin puts it back on the MSP's; leaving the list box unticked stops list prices.
    client.post("/admin/prices", data={"currency": "USD", "margin": "12", f"margin:{alpha}": ""})
    assert store.get_tenant(db, alpha)["price_margin"] is None
    assert not licences.uses_list_prices(db)
    actions = [r["action"] for r in db.execute("SELECT action FROM audit_log WHERE action LIKE 'price%'")]
    assert actions.count("price_margin_changed") == 3 and "prices_updated" in actions


def test_a_bad_margin_changes_nothing(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    _settings(db, **{"prices.margin": "8"})
    resp = client.post(
        "/admin/prices",
        data={"currency": "GBP", "margin": "lots", "new_client": str(alpha), "new_margin": "900"},
        follow_redirects=True,
    )
    page = resp.get_data(as_text=True)
    assert "Ignored the margin" in page and "Ignored the margin for Alpha Ltd" in page
    assert licences.msp_margin(db) == 8 and store.get_tenant(db, alpha)["price_margin"] is None


def test_priced_reports_tell_the_team_where_prices_came_from(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    _settings(db, currency="USD", **{"prices.margin": "12"})
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/data/licences").get_data(as_text=True)
    assert "Prices: Microsoft&#39;s list price (1 Jul 2026)" in page and "plus a 12% margin" in page
    # Downloads go to clients, so they never say it.
    workbook = client.get(f"/tenants/{alpha}/data/licences.xlsx").data
    assert b"margin" not in workbook
    built = data_reports.build(db, store.get_tenant(db, alpha), "licences")
    assert "margin" not in " ".join([built.footnote, *built.notes])


def test_a_client_never_sees_the_margin(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    _settings(db, currency="USD", **{"prices.margin": "12"})
    make_user(db, "someone", role="client", tenants=(alpha,))
    login(client, "someone")
    resp = client.get(f"/tenants/{alpha}/data/licences")
    assert "margin" not in resp.get_data(as_text=True)


def test_cached_rows_follow_a_margin_change(db, two_tenants):
    from officesentry.reports.precomputed import Rows

    alpha, _ = two_tenants
    tenant = store.get_tenant(db, alpha)
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    before = Rows(db, "rollup:licences", [tenant], now).fingerprint(tenant)
    _settings(db, **{"prices.margin": "12"})
    assert Rows(db, "rollup:licences", [tenant], now).fingerprint(tenant) != before
    middle = Rows(db, "rollup:licences", [tenant], now).fingerprint(tenant)
    db.execute("UPDATE tenants SET price_margin = 3 WHERE id = ?", (alpha,))
    tenant = store.get_tenant(db, alpha)
    assert Rows(db, "rollup:licences", [tenant], now).fingerprint(tenant) != middle
