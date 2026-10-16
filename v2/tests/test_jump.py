"""The jump box (web/jump.py): what it finds, how it ranks, and that it only ever shows what the person may open."""

import re
from pathlib import Path

from officesentry import roles, search_index, store
from officesentry.web.jump import PAGES, TABS, Thing, words_of
from tests.conftest import login, make_user
from tests.seed import seed_tenant
from tests.test_collectors import USERS
from tests.test_search import ZED

BASE = Path(__file__).resolve().parents[1] / "officesentry" / "web" / "templates" / "base.html"


def _ask(client, q, **params):
    resp = client.get("/jump", query_string={"q": q, **params})
    assert resp.status_code == 200 and resp.mimetype == "application/json", resp.status_code
    return resp.get_json()


def _rows(client, q, **params):
    return [(r["title"], r["where"], r["href"]) for r in _ask(client, q, **params)["rows"]]


def test_words_and_scores():
    assert words_of("Contoso Legal LLP contoso.onmicrosoft.com") == [
        "contoso",
        "legal",
        "llp",
        "contoso.onmicrosoft.com",
        "onmicrosoft",
        "com",
    ]
    mfa = Thing.of("MFA coverage", "multi-factor two-factor 2fa", weak="Who has MFA and who hasn't")
    assert mfa.score(["mfa"]) == 3 + 1  # the whole word, and it starts the name
    assert mfa.score(["cov"]) == 2  # the start of a word
    assert mfa.score(["erag"]) == 1  # inside a word
    assert mfa.score(["2fa"]) == 3 * 0.75  # a keyword counts for less
    assert mfa.score(["who"]) == 0.5  # a word of the description, for half a point
    assert mfa.score(["mfa", "coverage"]) == 3 + 3 + 1 + 0.5  # every word of the name covered
    assert mfa.score(["mfa", "xyz"]) is None  # every word typed must match
    users = Thing.of("Users")
    assert users.score(["users"]) == 3 + 1 + 0.5
    assert Thing.of("All users").score(["users"]) == 3  # not the first word, not the whole name
    # A report at a client: the words split between them, each side taking at least one.
    north = Thing.of("Northwind Traders northwind.example")
    assert north.split(mfa, ["north", "mfa"]) == (2 + 1) + (3 + 1) + 2
    assert north.split(mfa, ["mfa"]) is None
    assert north.split(mfa, ["north", "xyz"]) is None


def test_two_words_find_a_report_for_a_client(client, db, two_tenants):
    alpha, bravo = two_tenants
    make_user(db, "admin")
    login(client, "admin")

    rows = _rows(client, "alpha mfa")
    assert rows[0] == ("MFA coverage", "Alpha Ltd", f"/tenants/{alpha}/data/mfa")
    assert all(where == "Alpha Ltd" for _t, where, _h in rows)
    # A report alone opens across every client; on a client's page, that client comes first.
    assert _rows(client, "mfa")[0] == ("MFA coverage", "All clients", "/across/mfa")
    assert _rows(client, "mfa", tenant=bravo)[:2] == [
        ("MFA coverage", "Bravo Inc", f"/tenants/{bravo}/data/mfa"),
        ("MFA coverage", "All clients", "/across/mfa"),
    ]
    # A client, by name or domain, then its own pages and the summaries.
    first = _ask(client, "alpha")["rows"][0]
    assert (first["title"], first["href"], first["kind"]) == ("Alpha Ltd", f"/tenants/{alpha}", "client")
    assert first["detail"] == "Client · alpha.onmicrosoft.com"
    assert _rows(client, "bravo.onmicrosoft")[0][0] == "Bravo Inc"
    assert _rows(client, "alpha users")[0] == ("Users", "Alpha Ltd", f"/tenants/{alpha}/users")
    assert _rows(client, "bravo find")[0] == ("Findings", "Bravo Inc", f"/tenants/{bravo}/findings")
    assert _rows(client, "alpha changed")[0] == ("What changed", "Alpha Ltd", f"/tenants/{alpha}/changes")
    assert _rows(client, "alpha review")[0] == ("Monthly review", "Alpha Ltd", f"/tenants/{alpha}/report")
    # A word asking for a file gets the report's PDF or workbook, and nothing else; no report means the review.
    pdf = _ask(client, "alpha mfa pdf")["rows"]
    assert [(r["title"], r["where"], r["href"], r["kind"]) for r in pdf[:1]] == [
        ("Download MFA coverage", "Alpha Ltd", f"/tenants/{alpha}/data/mfa.pdf", "download")
    ]
    assert pdf[0]["detail"] == "PDF · Identity and access" and all(r["kind"] == "download" for r in pdf)
    assert _rows(client, "xlsx alpha mfa")[0][2] == f"/tenants/{alpha}/data/mfa.xlsx"
    assert [h for _t, _w, h in _rows(client, "bravo download")[:2]] == [
        f"/tenants/{bravo}/report.pdf",
        f"/tenants/{bravo}/report.xlsx",
    ]
    review = _rows(client, "pdf", tenant=alpha)[0]
    assert review == ("Download the monthly review", "Alpha Ltd", f"/tenants/{alpha}/report.pdf")
    assert _rows(client, "mfa pdf", tenant=bravo)[0][2] == f"/tenants/{bravo}/data/mfa.pdf"
    assert _ask(client, "pdf")["rows"] == []  # no client to download for
    # Pages and settings, by name or by what they hold.
    coming = _ask(client, "coming")["rows"][0]
    assert (coming["title"], coming["href"], coming["detail"]) == ("Coming up", "/upcoming", "Page")
    logo = _ask(client, "logo")["rows"][0]
    assert (logo["title"], logo["href"]) == ("Branding", "/admin/branding")
    assert logo["detail"] == "Settings · Reports and email"
    assert _rows(client, "retention")[0][2] == "/admin/retention"
    # Nothing typed, nothing matched: the full search is always a row away.
    assert _ask(client, "") == {"q": "", "rows": [], "more": None, "partial": False}
    gone = _ask(client, "xyzzy")
    assert gone["rows"] == [] and gone["more"] == "/search?q=xyzzy"
    assert len(_ask(client, "a")["rows"]) <= 10


def test_people_and_the_rest_come_from_the_search_index(app, client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, users=[*USERS, ZED])
    make_user(db, "admin")
    login(client, "admin")
    # Scanned, since the index isn't built yet...
    zed = next(r for r in _ask(client, "zed")["rows"] if r["title"] == "Zed Quinn")
    assert (zed["kind"], zed["where"], zed["href"]) == ("person", "Alpha Ltd", f"/tenants/{alpha}/users/u9")
    assert zed["detail"].startswith("Person, zed.quinn@x.com")
    # ...and the same through the index once the worker has built it.
    assert search_index.refresh(db, store.get_tenant(db, alpha))
    assert search_index.current(db, [store.get_tenant(db, alpha)]) == {alpha}
    assert next(r for r in _ask(client, "zed")["rows"] if r["title"] == "Zed Quinn") == zed
    # The person outranks a report whose words merely contain what was typed, and an IP address finds sign-ins.
    assert _rows(client, "ann@x.com")[0][0] == "Ann"
    assert any(r["kind"] == "signin" for r in _ask(client, "203.0.113")["rows"])
    assert any(r["kind"] == "domain" and r["title"] == "parked.com" for r in _ask(client, "parked")["rows"])


def test_only_what_the_person_may_open(client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha, users=[*USERS, ZED])
    make_user(db, "bravo-analyst", role="analyst", tenants=[bravo])
    login(client, "bravo-analyst")
    for q, extra in (("alpha", {}), ("alpha mfa", {}), ("zed", {}), ("mfa", {"tenant": alpha}), ("ann", {})):
        body = client.get("/jump", query_string={"q": q, **extra}).get_data(as_text=True)
        assert "Alpha" not in body and f"/tenants/{alpha}" not in body and "Zed" not in body, (q, body)
    assert _rows(client, "mfa", tenant=alpha)[0] == ("MFA coverage", "All clients", "/across/mfa")  # no context
    assert _rows(client, "bravo")[0][0] == "Bravo Inc"
    assert _ask(client, "branding")["rows"] == []  # not theirs to open
    assert _ask(client, "users")["rows"][0]["href"] != "/admin/users"
    client.post("/logout")

    # Someone without "across clients" gets each report on their own clients, and no full search.
    uid = make_user(db, "limited", role="analyst", tenants=[alpha, bravo])
    user = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    overrides = [roles.Override("clients.compare", None, False), roles.Override("export", None, False)]
    roles.set_overrides(db, "admin", user, overrides)
    login(client, "limited")
    assert _ask(client, "alpha mfa pdf")["rows"] == []  # no export, no files
    data = _ask(client, "mfa")
    assert data["more"] is None
    first_two = [(r["title"], r["where"]) for r in data["rows"][:2]]
    assert first_two == [("MFA coverage", "Alpha Ltd"), ("MFA coverage", "Bravo Inc")]
    assert not any(r["where"] == "All clients" for r in data["rows"])
    client.post("/logout")

    # Clients never get the box; signed out, the route asks for a sign-in.
    make_user(db, "viewer", role="client", tenants=[alpha])
    login(client, "viewer")
    assert client.get("/jump", query_string={"q": "alpha"}).status_code == 403
    client.post("/logout")
    resp = client.get("/jump", query_string={"q": "alpha"})
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


def test_the_box_is_on_staff_pages_only(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    make_user(db, "viewer", role="client", tenants=[alpha])
    login(client, "admin")
    home = client.get("/").get_data(as_text=True)
    assert 'id="jump"' in home and "jump.js" in home and 'data-src="/jump"' in home
    assert 'class="nav-search-btn" data-jump-open hidden' in home  # shown by jump.js in place of the form
    assert "data-tenant=" not in home
    assert f'data-tenant="{alpha}"' in client.get(f"/tenants/{alpha}").get_data(as_text=True)
    client.post("/logout")
    login(client, "viewer")
    theirs = client.get("/").get_data(as_text=True)
    assert 'id="jump"' not in theirs and "jump.js" not in theirs


def test_pages_match_the_menu_and_exist(app):
    """Every settings page base.html lists is a page the box finds, and every page and tab it names exists."""
    listed = set(re.findall(r"\('([a-z_]+\.[a-z_]+)', '[^']+', \[", BASE.read_text(encoding="utf-8")))
    known = {p.endpoint for p in PAGES}
    assert listed <= known, listed - known
    rules = {r.endpoint: r for r in app.url_map.iter_rules()}
    for page in PAGES:
        assert page.endpoint in rules and "GET" in rules[page.endpoint].methods and not rules[page.endpoint].arguments
    for tab in TABS:
        assert tab.endpoint in rules and rules[tab.endpoint].arguments == {"tenant_id"}, tab.endpoint
