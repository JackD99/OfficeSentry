"""Tenant tab bar (seven tabs, older page keys mapped onto them) and page landmarks."""

from __future__ import annotations

import re

import pytest

from officesentry.reports import data_reports
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
        f"/tenants/{alpha}/data/mailboxes": ("Reports", None),
        f"/tenants/{alpha}/evidence": ("Reports", None),
        f"/tenants/{alpha}/users": ("Users", "People"),
        _user_href(client, alpha): ("Users", "People"),
        f"/tenants/{alpha}/mailboxes": ("Users", "Mailboxes"),
        f"/tenants/{alpha}/mailboxes/u1": ("Users", "Mailboxes"),
        f"/tenants/{alpha}/schedules": ("Emails", None),
        f"/tenants/{alpha}/collection": ("Settings", "Data collection"),
        f"/tenants/{alpha}/manage": ("Settings", "Manage"),
        f"/tenants/{alpha}/offboard": ("Settings", "Manage"),
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


def test_report_picker_links_every_report(client, db, seeded):
    """The Reports tab has one picker: the summaries and every detail report, the open one marked."""
    alpha = seeded
    make_user(db, "admin")
    login(client, "admin")
    for url, current in (
        (f"/tenants/{alpha}/evidence", "Questionnaires and evidence"),
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
    assert re.findall(r'aria-current="page">([^<]+)<', _picker(page)) == ["Custom reports"]


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
