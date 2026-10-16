"""A link is only drawn where it opens: no page offers someone a link that answers 403.

Signed in as every built-in role and as a custom role limited to two clients (each with a capability added
or taken away on one client, as people's access is in practice), the portal is crawled from the landing
page and the main pages, following every same-site link and frame, and every link must open for the person
it was drawn for: a 403 (or a 500) fails, naming the page that offered it. ``web.access.can_open`` is what
templates ask, from the same checks the routes make; the unit tests below pin it down. A page's buttons
(its POST forms) aren't pressed, since that would change what the next walker sees: each must go to an
address whose checks let the person through, as ``can_open`` answers for it on the page's client.

The crawl opens a few URLs of each kind (the same route on the same client for the same report), so it
stays quick; downloads are fetched once per kind, with PDFs as the page they're made from.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

import pytest
import responses
from flask import g
from werkzeug.exceptions import HTTPException
from werkzeug.routing import RequestRedirect

from officesentry import delivery, issued, month_end, permissions, roles, store, tenant_groups
from officesentry.certs import generate_certificate
from officesentry.config import load_config
from officesentry.crypto import Vault
from officesentry.db import connect, migrate, utcnow
from officesentry.reports import data_reports, designs, export
from officesentry.web import create_app
from officesentry.web.access import can_open
from tests.conftest import PASSWORD_HASH, login, make_user
from tests.seed import seed_tenant

# Who walks: every built-in role, and a custom one. GRANTED roles are given Alpha and Bravo (not Charlie).
BUILT_IN = [t.key for t in permissions.TEMPLATES]
CUSTOM = "limited"
WALKERS = [*BUILT_IN, CUSTOM]
# Per-person changes on one client, like the demo's: first line refreshes Alpha only, the account manager
# opens people on Alpha only, the custom role has email reports taken away on Bravo.
OVERRIDES = {
    "first_line": [("collect", "alpha", True)],
    "account_manager": [("reports.people", "alpha", True)],
    CUSTOM: [("reports.email", "bravo", False)],
}
CUSTOM_CAPS = (
    "reports.security",
    "reports.identity",
    "reports.email",
    "reports.people",
    "emails.view",
    "export",
    "clients.compare",
    "collect",
    "emails.manage",
)
DOWNLOADS = (".pdf", ".xlsx", ".csv", ".docx", ".txt", ".zip", ".cer")
# Not followed: signing out or in, Microsoft's sign-in, static files and the health checks.
NOT_FOLLOWED = {"auth.logout", "auth.login", "sso.start", "sso.callback", "static", "health", "health_ready"}
# URL arguments that decide whether a page opens (the client, which report, which scheme); the rest (which
# person, group, schedule or design) only pick one of a kind.
DECIDING = ("tenant_id", "key", "scheme")
PAGES_PER_KIND = 2
DOWNLOADS_PER_KIND = 1
MAX_PAGES = 2500
DEAD = (403, 500)


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []
        self.forms: list[str] = []  # where each button (a POST form) sends

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "iframe" and a.get("src"):
            self.links.append(a["src"])
        elif tag == "form" and (a.get("method") or "").lower() == "post":
            self.forms.append(a.get("action") or "")


def links_on(page: str) -> _Links:
    parser = _Links()
    parser.feed(page)
    return parser


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """Three clients (Alpha and Bravo collected, Charlie not yet), one of everything a page links to (a
    schedule, alert, contact, delivery and issued report per client, a report plan, a design, a client group,
    a saved view), and an account for each walker."""
    with pytest.MonkeyPatch.context() as mp:
        for var in ("SECRET_KEY", "ENCRYPTION_KEYS", "DATA_DIR"):
            mp.delenv("OFFICESENTRY_" + var, raising=False)
        cfg = load_config(
            data_dir=tmp_path_factory.mktemp("links"),
            testing=True,
            secure_cookies=False,
            require_mfa="off",
            extra={"disable_csrf": True},
        )
    db = connect(cfg.db_path)
    migrate(db)
    vault = Vault(cfg.encryption_keys)
    cid = store.create_connection(
        db, vault, "Test app", generate_certificate("test"), client_id="11111111-2222-3333-4444-555555555555"
    )
    ids = {
        name: store.create_tenant(db, name.title() + " Ltd", f"{name}.onmicrosoft.com", cid)
        for name in ("alpha", "bravo", "charlie")
    }
    db.execute("UPDATE tenants SET consented_at = ?", (utcnow(),))
    seed_tenant(db, ids["alpha"])
    seed_tenant(db, ids["bravo"])
    _objects(db, ids)
    _walkers(db, ids)
    db.commit()
    app = create_app(cfg)
    yield app, db, ids
    db.close()


def _objects(db, ids: dict) -> None:
    now = utcnow()
    for word in ("alpha", "bravo"):
        tenant = ids[word]
        schedule = db.execute(
            "INSERT INTO email_schedules (kind, tenant_id, reports, frequency, hour, timezone, created_at)"
            " VALUES ('report', ?, '[\"review\", \"mfa\"]', 'monthly', 8, 'UTC', ?)",
            (tenant, now),
        ).lastrowid
        db.execute(
            "INSERT INTO email_alerts (tenant_id, min_severity, created_at) VALUES (?, 'high', ?)", (tenant, now)
        )
        contact = delivery.add_contact(db, tenant, f"{word} IT", f"it@{word}.example")
        db.execute("INSERT INTO email_schedule_recipients (schedule_id, contact_id) VALUES (?, ?)", (schedule, contact))
        db.execute(
            "INSERT INTO email_deliveries (kind, schedule_id, tenant_id, subject, recipients, status, error,"
            " created_at) VALUES ('report', ?, ?, 'Monthly', ?, 'failed', 'x', ?)",
            (schedule, tenant, f"it@{word}.example", now),
        )
        issued.archive(
            db,
            tenant,
            [issued.IssuedFile("review", "Monthly review", f"{word}.pdf", "application/pdf", b"%PDF-1 x")],
            period="2026-09",
            trigger="manual",
            user="x",
        )
    db.execute(
        "INSERT INTO email_schedules (kind, tenant_id, reports, frequency, hour, timezone, created_at)"
        " VALUES ('digest', NULL, '[\"review\"]', 'weekly', 8, 'UTC', ?)",
        (now,),
    )
    plan = month_end.save_plan(
        db,
        {
            "name": "Monthly",
            "reports": ["review"],
            "include_xlsx": 0,
            "day_of_month": 1,
            "hour": 8,
            "timezone": "UTC",
            "subject": "{client}: {month}",
            "message": "",
            "mode": "approval",
            "delivery": "email",
            "bcc_team": 0,
        },
        "test",
    )
    month_end.apply_plan(db, month_end.get_plan(db, plan), [ids["alpha"], ids["bravo"]], "test", "UTC")
    designs.create(db, "Board pack", "test", designs.STARTERS["executive"][2])
    group = tenant_groups.create(db, {"name": "North", "category": "Region", "description": ""}, "test")
    tenant_groups.set_members(db, group, [ids["alpha"], ids["bravo"]])


def _walkers(db, ids: dict) -> None:
    granted = (ids["alpha"], ids["bravo"])
    for key in WALKERS:
        if key == CUSTOM:
            role = roles.get(
                db, roles.create(db, "test", "Two clients", "", permissions.STAFF, permissions.GRANTED, CUSTOM_CAPS)
            )
        else:
            role = roles.by_key(db, key)
        uid = db.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (key, PASSWORD_HASH, role.column, utcnow()),
        ).lastrowid
        for tenant in granted[:1] if role.kind == permissions.CLIENT else granted:
            db.execute("INSERT INTO user_tenants (user_id, tenant_id) VALUES (?, ?)", (uid, tenant))
        roles.assign(db, uid, role)
        if key in OVERRIDES:
            user = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
            changes = [roles.Override(cap, ids[where], allow) for cap, where, allow in OVERRIDES[key]]
            roles.set_overrides(db, "test", user, changes)


@pytest.fixture
def _pdf_as_page(monkeypatch):
    """PDFs come back as the page WeasyPrint is handed, so the crawl doesn't spend minutes typesetting."""
    monkeypatch.setattr(export, "to_pdf", lambda html, base_url: html.encode())


def _start(ids: dict) -> list[str]:
    """The landing page and the main pages: the menu's, and each client's tabs."""
    pages = [
        "/home",
        "/",
        "/today",
        "/clients",
        "/emails",
        "/upcoming",
        "/across",
        "/across/checks",
        "/changes",
        "/reports",
        "/reporting",
        "/reporting/plans",
        "/reporting/designs",
        "/projects",
        "/search?q=a",
        "/admin/users",
        "/admin/groups",
        "/admin/tenants",
        "/admin/activity",
        "/account",
    ]
    for tenant in ids.values():
        pages += [
            f"/tenants/{tenant}{tab}"
            for tab in ("", "/findings", "/changes", "/data", "/report", "/evidence", "/users", "/schedules")
        ]
        pages += [f"/tenants/{tenant}/{tab}" for tab in ("mailboxes", "groups", "sites", "designs", "projects")]
    return pages


def _kind(app, path: str, method: str = "GET"):
    """Which route on which client for which report: URLs of one kind open alike (or None: no route)."""
    try:
        endpoint, args = app.url_map.bind("localhost").match(unquote(path), method=method)
    except RequestRedirect:
        return ("redirect", path)
    except HTTPException:
        return None
    return endpoint, tuple((k, args[k]) for k in DECIDING if k in args)


def _local(url) -> str | None:
    """A same-site URL as a path and query, or None."""
    if url.scheme not in ("", "http", "https") or url.netloc not in ("", "localhost"):
        return None
    return url.path + (f"?{url.query}" if url.query else "")


def _client_of(app, path: str) -> int | None:
    """The client a page's URL names, if it names one."""
    try:
        return app.url_map.bind("localhost").match(unquote(path), method="GET")[1].get("tenant_id")
    except (HTTPException, RequestRedirect):
        return None


def crawl(app, client, ids: dict) -> tuple[list[tuple[str, int, str]], int, list[tuple[str, str]]]:
    """Every link reachable from the start pages: (url, status, the page that linked it) for one link of
    each kind on each kind of page that answered 403 or 500, how many URLs were opened, and one button of
    each kind on each client (where it sends, the page it's on). Once a few URLs of a kind are opened, the
    rest of that kind answer as they did."""
    queue = deque((url, None) for url in _start(ids))
    answered: dict[str, int] = {}  # every URL opened, and its status
    kinds: dict[tuple, list[int]] = {}  # what each kind of URL answered
    bad: dict[tuple, tuple[str, int, str]] = {}
    buttons: dict[tuple, tuple[str, str]] = {}
    while queue and len(answered) < MAX_PAGES:
        target, parent = queue.popleft()
        path = urlsplit(target).path
        kind = _kind(app, path)
        if kind is None or kind[0] in NOT_FOLLOWED:
            continue
        download = path.endswith(DOWNLOADS)
        statuses = kinds.setdefault(kind, [])
        resp = None
        if target in answered:
            status = answered[target]
        elif len(statuses) >= (DOWNLOADS_PER_KIND if download else PAGES_PER_KIND):
            status = next((s for s in statuses if s in DEAD), statuses[0])
        else:
            resp = client.get(target)
            status = answered[target] = resp.status_code
            statuses.append(status)
        if parent is not None and status in DEAD:
            bad.setdefault((kind, _kind(app, urlsplit(parent).path)), (target, status, parent))
        if resp is None:
            continue
        if status in (301, 302, 303, 307, 308):
            location = _local(urlsplit(urljoin("http://localhost" + target, resp.headers.get("Location", ""))))
            if location is not None:
                assert urlsplit(location).path != "/login", f"signed out by {target}"
                queue.append((location, parent))  # a link that redirects is the page it lands on
            continue
        if status != 200 or download or "text/html" not in resp.headers.get("Content-Type", ""):
            continue
        page = links_on(resp.get_data(as_text=True))
        for href in page.links:
            if not href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
                if (link := _local(urlsplit(urljoin("http://localhost" + target, href)))) is not None:
                    queue.append((link, target))
        for action in page.forms:
            if (sends := _local(urlsplit(urljoin("http://localhost" + target, action)))) is not None:
                kind = _kind(app, urlsplit(sends).path, "POST") or ("nowhere", sends)
                buttons.setdefault((kind, _client_of(app, path)), (sends, target))
    return list(bad.values()), len(answered), list(buttons.values())


@pytest.mark.parametrize("walker", WALKERS)
def test_no_page_offers_a_link_that_answers_403(world, walker, _pdf_as_page):
    app, db, ids = world
    client = app.test_client()
    assert login(client, walker).status_code == 302
    with responses.RequestsMock():  # no page reaches Microsoft (or anywhere) while it's opened
        bad, count, buttons = crawl(app, client, ids)
    assert count > 60, count  # the crawl reached the portal, not just the sign-in page
    assert not bad, f"{walker} was offered {len(bad)} link(s) that don't open:\n" + "\n".join(
        f"  {status} {url}  (on {parent})" for url, status, parent in bad
    )
    refused = []
    with signed_in(app, db, walker):
        for sends, page in buttons:
            try:
                endpoint, values = app.url_map.bind("localhost").match(unquote(urlsplit(sends).path), method="POST")
            except HTTPException:
                refused.append((sends, page))  # a button that goes nowhere
                continue
            if not can_open(endpoint, tenant=_client_of(app, urlsplit(page).path), **values):
                refused.append((sends, page))
    assert not refused, f"{walker} was offered {len(refused)} button(s) that answer 403:\n" + "\n".join(
        f"  {sends}  (on {page})" for sends, page in refused
    )


@contextmanager
def signed_in(app, db, username: str | None):
    """A request as ``username`` (None: nobody signed in), to ask what a template would. The request has its
    own connection, as a page does, which the app closes at the end."""
    db.commit()
    with app.test_request_context():
        g.db = connect(db.execute("PRAGMA database_list").fetchone()["file"])
        g.user = g.db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone() if username else None
        yield


def _person(db, username: str, template: str, tenants, overrides=()) -> None:
    """An account on a built-in role, granted these clients, with per-client changes (capability, client, allow)."""
    uid = make_user(db, username, role="client" if template == "client_viewer" else "analyst", tenants=tenants)
    roles.assign(db, uid, roles.by_key(db, template))
    if overrides:
        user = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        roles.set_overrides(db, "test", user, [roles.Override(cap, t, allow) for cap, t, allow in overrides])


def test_can_open_answers_as_the_page_would(app, db, two_tenants, connection_id):
    """What a page declared (its capabilities, its kind, the report its URL names) and its client's checks
    (seen at all, a capability added or taken away there), asked by endpoint or by path."""
    alpha, bravo = two_tenants
    charlie = store.create_tenant(db, "Charlie Co", "charlie.onmicrosoft.com", connection_id)
    _person(db, "am", "account_manager", (alpha, bravo), [("reports.people", alpha, True)])
    _person(db, "aud", "auditor", (alpha, bravo), [("reports.identity", bravo, False)])
    _person(db, "viewer", "client_viewer", (alpha,))

    with signed_in(app, db, "am"):
        assert can_open("reports.tenant_findings", tenant_id=alpha)  # reports.security
        assert not can_open("reports.tenant_findings", tenant_id=charlie)  # a client they don't see
        assert can_open(f"/tenants/{alpha}/users/u1")  # people's pages: added on Alpha only
        assert not can_open(f"/tenants/{bravo}/users/u1")
        assert not can_open(f"/tenants/{alpha}/data/mfa?show=all")  # the report needs reports.identity
        assert not can_open("tenants.collection", tenant_id=alpha)
        assert not can_open("admin.users")
        assert can_open("/account")  # anyone signed in
        assert not can_open("/no/such/page")
        assert not can_open("/schedules/1/send")  # a form's address, not a page
    with signed_in(app, db, "aud"):
        assert can_open(f"/tenants/{alpha}/data/mfa")
        assert not can_open(f"/tenants/{bravo}/data/mfa")  # taken away on Bravo
        assert can_open(f"/tenants/{bravo}/data/mailbox-size")
        assert can_open("email.preview_schedule", schedule_id=1, tenant=alpha)
        assert not can_open("email.edit_schedule", schedule_id=1, tenant=alpha)  # emails.manage
        assert not can_open("designs.edit", design_id=1)
    with signed_in(app, db, "viewer"):
        assert can_open(f"/tenants/{alpha}/data/mfa")
        assert not can_open(f"/tenants/{bravo}/data/mfa")
        assert not can_open("email.preview_schedule", schedule_id=1, tenant=alpha)  # a staff page
    with signed_in(app, db, None):
        assert not can_open("/account")


def test_a_report_names_a_person_as_text_where_their_page_doesnt_open(app, db, two_tenants):
    """A report row's name links to the person's page only for someone who opens it; for anyone else it's
    the name alone, in a single cell and in a list."""
    alpha, bravo = two_tenants
    _person(db, "am", "account_manager", (alpha, bravo), [("reports.people", alpha, True)])
    on_alpha = data_reports.UserLink("Ann Lee", data_reports.user_path(alpha, "u1"))
    on_bravo = data_reports.UserLink("Bob Ray", data_reports.user_path(bravo, "u2"))
    both = data_reports.Many(["Ann Lee", "Bob Ray"])
    both.hrefs = {"Ann Lee": on_alpha.page_href, "Bob Ray": on_bravo.page_href}

    with signed_in(app, db, "am"):
        cell = app.jinja_env.get_template("_report_table.html").module.cell
        assert str(cell(on_alpha)) == f'<a href="/tenants/{alpha}/users/u1">Ann Lee</a>'
        assert str(cell(on_bravo)) == "Bob Ray"
        assert str(cell(both)) == f'<a href="/tenants/{alpha}/users/u1">Ann Lee</a>, Bob Ray'
