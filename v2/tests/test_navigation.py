"""Tenant tab bar (seven tabs, older page keys mapped onto them) and page landmarks."""

from __future__ import annotations

import re

import pytest

from officesentry.reports import data_reports, designs
from tests import auditdata, autodeskdata
from tests import sharepoint_data as sd
from tests.conftest import login, make_user
from tests.seed import seed_tenant

TABS = ["Overview", "Findings", "Changes", "Reports", "Users", "Emails", "Settings"]


def _tab_bar(page: str) -> str:
    bars = re.findall(r'<nav class="tabs"[^>]*>(.*?)</nav>', page, re.S)
    assert len(bars) == 1
    return bars[0]


def _labels(bar: str) -> list[str]:
    return [re.sub(r"<[^>]+>.*", "", a).strip() for a in re.findall(r"<a [^>]*>(.*?)</a>", bar, re.S)]


def _active(bar: str) -> list[str]:
    return [
        re.sub(r"<[^>]+>.*", "", a).strip() for a in re.findall(r'<a [^>]*class="active"[^>]*>(.*?)</a>', bar, re.S)
    ]


def _pages(page: str) -> list[str]:
    """Second-row links (Reports, Settings) and which one is open."""
    rows = re.findall(r'<nav class="tab-pages"[^>]*>(.*?)</nav>', page, re.S)
    return _active(rows[0]) if rows else []


@pytest.fixture
def seeded(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    return alpha


def _user_href(client, alpha):
    page = client.get(f"/tenants/{alpha}/users").get_data(as_text=True)
    return re.search(rf'href="(/tenants/{alpha}/users/[^".]+)"', page).group(1)


def _run_href(client, alpha):
    page = client.get(f"/tenants/{alpha}/collection").get_data(as_text=True)
    return re.search(r'href="(/runs/\d+)"', page).group(1)


def test_every_tenant_page_opens_one_tab(client, db, seeded):
    alpha = seeded
    make_user(db, "admin")
    login(client, "admin")
    pages = {
        f"/tenants/{alpha}": ("Overview", None),
        f"/tenants/{alpha}/findings": ("Findings", None),
        f"/tenants/{alpha}/projects": ("Findings", None),
        f"/tenants/{alpha}/changes": ("Changes", "What changed"),
        f"/tenants/{alpha}/activity": ("Changes", "Activity history"),
        f"/tenants/{alpha}/timeline": ("Changes", "Timeline"),
        f"/tenants/{alpha}/data": ("Reports", None),
        f"/tenants/{alpha}/data/mailboxes": ("Reports", None),
        f"/tenants/{alpha}/evidence": ("Reports", None),
        f"/tenants/{alpha}/users": ("Users", "People"),
        _user_href(client, alpha): ("Users", "People"),
        f"/tenants/{alpha}/mailboxes": ("Users", "Mailboxes"),
        f"/tenants/{alpha}/mailboxes/u1": ("Users", "Mailboxes"),
        f"/tenants/{alpha}/groups": ("Users", "Groups and teams"),
        f"/tenants/{alpha}/groups/g1": ("Users", "Groups and teams"),
        f"/tenants/{alpha}/sites": ("Users", "Sites"),
        f"/tenants/{alpha}/sites/{sd.SITE_USAGE[0]['siteId']}": ("Users", "Sites"),
        f"/tenants/{alpha}/schedules": ("Emails", None),
        f"/tenants/{alpha}/collection": ("Settings", "Data collection"),
        f"/tenants/{alpha}/manage": ("Settings", "Manage"),
        f"/tenants/{alpha}/offboard": ("Settings", "Manage"),
        f"/tenants/{alpha}/report-history": ("Emails", None),
        f"/tenants/{alpha}/schedules/new": ("Emails", None),
        _run_href(client, alpha): ("Settings", "Data collection"),
    }
    for url, (tab, sub) in pages.items():
        resp = client.get(url)
        assert resp.status_code == 200, url
        page = resp.get_data(as_text=True)
        bar = _tab_bar(page)
        assert _labels(bar) == TABS, url
        assert _active(bar) == [tab], url
        assert _pages(page) == ([sub] if sub else []), url
        assert page.count('aria-current="page"') >= 1, url


def _picker(page: str) -> str:
    pickers = re.findall(r'<nav class="report-menu"[^>]*>(.*?)</nav>', page, re.S)
    assert len(pickers) == 1
    return pickers[0]


def test_report_picker_links_every_report(client, db, vault, seeded):
    """The Reports tab has one picker: the summaries and every detail report, the open one marked."""
    alpha = seeded
    auditdata.everywhere(db, vault)  # the add-ons' reports too
    autodeskdata.everywhere(db, vault)
    make_user(db, "admin")
    login(client, "admin")
    for url, current in (
        (f"/tenants/{alpha}/evidence", "Evidence and certifications"),
        (f"/tenants/{alpha}/data/inbox-rules", "Inbox rules"),
    ):
        page = client.get(url).get_data(as_text=True)
        picker = _picker(page)
        hrefs = re.findall(r'href="(/tenants/[^"]+)"', picker)
        assert f"/tenants/{alpha}/report" in hrefs and f"/tenants/{alpha}/evidence" in hrefs
        assert f"/tenants/{alpha}/designs" in hrefs  # Custom reports, for staff
        assert {f"/tenants/{alpha}/data/{key}" for key in data_reports.REPORTS} <= set(hrefs)
        assert len(hrefs) == len(data_reports.REPORTS) + 3
        assert _active(picker) == [] and re.findall(r'aria-current="page">([^<]+)<', picker) == [current]
        assert 'data-report-search="report-list"' in picker and '<label for="report-find"' in picker
        # Narrower screens get the same list as a select, which the Reports tab URL turns into the page.
        assert '<option value="evidence"' in page and '<optgroup label="Identity and access">' in page
    assert client.get(f"/tenants/{alpha}/data?key=evidence").headers["Location"].endswith(f"/tenants/{alpha}/evidence")
    assert client.get(f"/tenants/{alpha}/data?key=review").headers["Location"].endswith(f"/tenants/{alpha}/report")
    assert client.get(f"/tenants/{alpha}/data?key=designs").headers["Location"].endswith(f"/tenants/{alpha}/designs")
    page = client.get(f"/tenants/{alpha}/designs").get_data(as_text=True)
    picker = _picker(page)
    assert re.findall(r'aria-current="page">([^<]+)<', picker) == ["Custom reports"]
    assert "Custom reports" in picker and 'href="/reporting/designs"' in picker  # the group, and making one
    assert "Your views" not in picker  # nothing saved yet


def test_report_picker_lists_custom_reports_and_saved_views(client, db, seeded):
    """The MSP's custom reports and the person's saved views join the menu, and the phone select's keys for
    them open the same pages."""
    alpha = seeded
    make_user(db, "admin")
    login(client, "admin")
    did = designs.create(db, "Board pack", "admin", designs.STARTERS["executive"][2], "For the board")
    client.post(
        "/reports/mailboxes/views",
        data={"name": "Leaving us", "query": "f=forwarding/outside-the-organisation:Yes", "tenant_id": alpha},
    )
    vid = db.execute("SELECT id FROM saved_views").fetchone()[0]
    view_href = f"/tenants/{alpha}/data/mailboxes?f=forwarding%2Foutside-the-organisation%3AYes"
    picker = _picker(client.get(f"/tenants/{alpha}/data/inbox-rules").get_data(as_text=True))
    assert f'href="/tenants/{alpha}/designs/{did}" title="For the board"' in picker
    assert f'href="{view_href}"' in picker and "Leaving us" in picker and "Your views" in picker
    assert ">All custom reports<" in picker and "New custom report</a>" in picker
    page = client.get(f"/tenants/{alpha}/designs/{did}").get_data(as_text=True)
    assert f'<option value="design:{did}" selected>Board pack</option>' not in page  # a report page, no picker
    page = client.get(f"/tenants/{alpha}/data/inbox-rules").get_data(as_text=True)
    assert f'<option value="design:{did}">Board pack</option>' in page
    assert f'<option value="view:{vid}">Leaving us · Mail forwarding</option>' in page
    assert (
        client.get(f"/tenants/{alpha}/data?key=design:{did}")
        .headers["Location"]
        .endswith(f"/tenants/{alpha}/designs/{did}")
    )
    assert client.get(f"/tenants/{alpha}/data?key=view:{vid}").headers["Location"].endswith(view_href)
    assert client.get(f"/tenants/{alpha}/data?key=design:999").status_code == 404
    assert client.get(f"/tenants/{alpha}/data?key=view:999").status_code == 404
    # Another person's view isn't theirs to open, and a client account sees no custom reports.
    make_user(db, "other", role="analyst", all_tenants=True)
    client.post("/logout")
    login(client, "other")
    assert client.get(f"/tenants/{alpha}/data?key=view:{vid}").status_code == 404
    client.post("/logout")
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    page = client.get(f"/tenants/{alpha}/data/inbox-rules").get_data(as_text=True)
    assert "Custom reports" not in page and "Board pack" not in page
    assert client.get(f"/tenants/{alpha}/data?key=design:{did}").status_code == 404


def _directory(page: str) -> str:
    return page.split('id="report-directory"')[1].split("</section>")[0]


def test_reports_tab_opens_on_the_catalogue(client, db, vault, seeded):
    """The Reports tab is the client's report catalogue: the summaries, the MSP's custom reports and the
    person's saved views, then every detail report by area, with one search box; the overview's Reports card
    is the same list. A key that isn't there shows it too."""
    alpha = seeded
    auditdata.everywhere(db, vault)  # the add-ons' reports too
    autodeskdata.everywhere(db, vault)
    make_user(db, "admin")
    login(client, "admin")
    did = designs.create(db, "Board pack", "admin", designs.STARTERS["executive"][2], "For the board")
    client.post(
        "/reports/mailboxes/views",
        data={"name": "Leaving us", "query": "f=forwarding/outside-the-organisation:Yes", "tenant_id": alpha},
    )
    for url in (f"/tenants/{alpha}/data", f"/tenants/{alpha}", f"/tenants/{alpha}/data?key=nothing"):
        page = client.get(url).get_data(as_text=True)
        directory = _directory(page)
        hrefs = re.findall(r'href="(/tenants/[^"]+)"', directory)
        assert hrefs[:3] == [f"/tenants/{alpha}/report", f"/tenants/{alpha}/evidence", f"/tenants/{alpha}/projects"]
        assert {f"/tenants/{alpha}/data/{key}" for key in data_reports.REPORTS} <= set(hrefs)
        assert f"/tenants/{alpha}/designs/{did}" in hrefs and f"/tenants/{alpha}/designs" in hrefs
        assert f"/tenants/{alpha}/data/mailboxes?f=forwarding%2Foutside-the-organisation%3AYes" in hrefs
        assert "New custom report" in directory and 'href="/reporting/designs"' in directory
        assert 'data-report-search="report-directory"' in page and ">Your views</h3>" in directory
    page = client.get(f"/tenants/{alpha}/data").get_data(as_text=True)
    assert _active(_tab_bar(page)) == ["Reports"] and '<nav class="report-menu"' not in page
    assert re.search(r"\d+ reports on .*'s data\. Pick one, or search", page)
    # A client account gets its own areas, and neither custom reports nor anyone's views.
    make_user(db, "client", role="client", tenants=[alpha])
    client.post("/logout")
    login(client, "client")
    directory = _directory(client.get(f"/tenants/{alpha}/data").get_data(as_text=True))
    assert f"/tenants/{alpha}/report" in directory and f"/tenants/{alpha}/data/mailboxes" in directory
    assert "Custom reports" not in directory and "Your views" not in directory and "Board pack" not in directory


def test_tenant_header_pins_its_tab_bar(client, db, seeded):
    alpha = seeded
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}").get_data(as_text=True)
    head = re.search(r'<header class="tenant-head" data-pin>(.*?)</header>', page, re.S).group(1)
    assert '<nav class="tabs"' in head and '<span class="pin-name" aria-hidden="true">' in head


@pytest.mark.parametrize("role", ["analyst", "client"])
def test_tabs_follow_the_role(client, db, seeded, role):
    alpha = seeded
    make_user(db, "someone", role=role, tenants=[alpha])
    login(client, "someone")
    page = client.get(f"/tenants/{alpha}/findings").get_data(as_text=True)
    labels = _labels(_tab_bar(page))
    if role == "client":
        assert labels == ["Findings", "Changes", "Reports", "Users", "Emails"]
        assert f"/tenants/{alpha}/collection" not in page and f"/tenants/{alpha}/manage" not in page
        reports = client.get(f"/tenants/{alpha}/evidence").get_data(as_text=True)
        assert f"/tenants/{alpha}/designs" not in reports and 'value="designs"' not in reports
    else:
        assert labels == TABS


def test_landmarks(client, db, seeded):
    alpha = seeded
    make_user(db, "admin")
    login(client, "admin")
    report = client.get(f"/tenants/{alpha}/report").get_data(as_text=True)
    assert report.count("<main") == 1 and report.count("</main>") == 1
    person = client.get(_user_href(client, alpha)).get_data(as_text=True)
    asides = re.findall(r"<aside[^>]*>", person)
    assert len(asides) == 2
    labels = [re.search(r'aria-label="([^"]+)"', a).group(1) for a in asides]
    assert len(set(labels)) == 2
    assert 'class="skip-link" href="#main"' in person and '<main id="main"' in person
    dashboard = client.get("/").get_data(as_text=True)
    assert re.search(r'<span class="sevbar" role="img" aria-label="\d+ critical, \d+ high', dashboard)


def _menu(page: str) -> tuple[list[str], list[str]]:
    """The highlighted entries of the sidebar's menu, and of its account links."""
    side = page[page.index('<aside class="sidebar"') : page.index("</aside>")]
    nav = side[side.index('<nav class="nav"') : side.index("</nav>")]
    account = side[side.index('<div class="account">') :]

    def lit(part: str) -> list[str]:
        links = re.findall(r'<a [^>]*aria-current="page"[^>]*>(.*?)</a>', part, re.S)
        return [re.sub(r"<[^>]+>", "", a).strip() for a in links]

    return lit(nav), lit(account)


def test_every_page_lights_the_menu_entry_it_belongs_to(client, db, seeded):
    """The menu highlights the entry a page lives under, by its path, so a page a tab or a list opens (a person,
    resolved findings, a run, a schedule, a settings page's sub-page) keeps its section lit."""
    alpha = seeded
    make_user(db, "admin")
    login(client, "admin")
    collection = client.get(f"/tenants/{alpha}/collection").get_data(as_text=True)
    run = re.search(r'href="(/runs/\d+)"', collection).group(1)
    pages = {
        "/": "Clients",
        "/home": "Home",
        "/today": "Home",
        "/clients": "Home",
        f"/tenants/{alpha}/users": "Clients",
        _user_href(client, alpha): "Clients",
        f"/tenants/{alpha}/findings/resolved": "Clients",
        f"/tenants/{alpha}/activity": "Clients",
        f"/tenants/{alpha}/sign-in": "Clients",
        f"/tenants/{alpha}/report-history": "Clients",
        f"/tenants/{alpha}/designs": "Clients",
        f"/tenants/{alpha}/schedules/new": "Clients",
        "/tenants/new": "Clients",
        run: "Clients",
        "/reports": "Reports",
        "/reporting/designs": "Reports",
        "/across": "Clients",
        "/across/mfa": "Clients",
        "/across/mfa/rows": "Clients",
        "/across/checks": "Clients",
        "/search?q=alpha": "Clients",
        "/changes": "What changed",
        "/upcoming": "Coming up",
        "/emails": "Email reports",
        "/emails/digests/new": "Email reports",
        "/emails/views/new": "Email reports",
        "/reporting": "Month-end",
        "/reporting/plans/new": "Month-end",
        "/projects": "Projects",
        "/admin/roles": "Settings",
        "/admin/onboarding": "Settings",
    }
    for url, entry in pages.items():
        resp = client.get(url, follow_redirects=True)
        assert resp.status_code == 200, url
        menu, account = _menu(resp.get_data(as_text=True))
        assert menu == [entry] and account == [], url
    for url in ("/account", "/account/password", "/two-step/setup"):
        resp = client.get(url, follow_redirects=True)
        assert resp.status_code == 200, url
        menu, account = _menu(resp.get_data(as_text=True))
        assert menu == [] and account == ["Account"], url
    client.post("/logout")
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    for url, entry in {
        "/": "Your reports",
        f"/tenants/{alpha}/findings": "Your reports",
        "/reports": "All reports",
    }.items():
        resp = client.get(url, follow_redirects=True)
        assert resp.status_code == 200, url
        assert _menu(resp.get_data(as_text=True)) == ([entry], []), url


def test_a_page_opened_from_a_list_says_where_it_came_from(client, db, seeded):
    """A client page that isn't a tab or a second-row page shows "← <the page it was opened from>" above
    its title, as a person's page shows "← People"; a page that isn't a client's shows crumbs from its
    menu entry ending with itself, and no page has both crumbs and a Back button."""
    alpha = seeded
    make_user(db, "admin")
    login(client, "admin")
    backs = {
        _run_href(client, alpha): f'<a href="/tenants/{alpha}/collection">&larr; Data collection</a>',
        f"/tenants/{alpha}/schedules/new": f'<a href="/tenants/{alpha}/schedules">&larr; Emailed reports</a>',
        f"/tenants/{alpha}/report-history": f'<a href="/tenants/{alpha}/schedules">&larr; Emailed reports</a>',
        f"/tenants/{alpha}/offboard": f'<a href="/tenants/{alpha}/manage">&larr; Manage</a>',
        f"/tenants/{alpha}/projects": f'<a href="/tenants/{alpha}/findings">&larr; Findings</a>',
    }
    for url, back in backs.items():
        page = client.get(url).get_data(as_text=True)
        assert f'<div class="crumbs">{back}</div>' in page, url
        assert page.count("<h1") == 1 and f"<h1>{'Alpha Ltd'}</h1>" in page, url  # the client's name
        assert ">Back</a>" not in page, url
    sid = db.execute(
        "INSERT INTO email_schedules (kind, tenant_id, reports, include_xlsx, frequency, day_of_week, day_of_month,"
        " hour, timezone, created_at) VALUES ('report', ?, '[\"review\"]', 1, 'monthly', 0, 1, 8, 'UTC', ?)",
        (alpha, "2026-01-01T00:00:00Z"),
    ).lastrowid
    db.commit()
    for url in (f"/schedules/{sid}/edit", f"/schedules/{sid}/preview"):
        page = client.get(url).get_data(as_text=True)
        assert f'<div class="crumbs"><a href="/tenants/{alpha}/schedules">&larr; Emailed reports</a></div>' in page, url
        assert _active(_tab_bar(page)) == ["Emails"] and ">Back</a>" not in page, url
    digest = client.get("/emails/digests/new").get_data(as_text=True)
    assert (
        '<div class="crumbs"><a href="/emails">Email reports</a><span>/</span><span>New digest</span></div>' in digest
    )
    plan = client.get("/reporting/plans/new").get_data(as_text=True)
    assert (
        "<span>/</span><span>New report plan</span></div>" in plan
        and 'href="/reporting/plans">Report plans</a>' in plan
    )
