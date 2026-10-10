"""Tenant tab bar (seven tabs, older page keys mapped onto them) and page landmarks."""

from __future__ import annotations

import re

import pytest

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
    return [re.sub(r"<[^>]+>.*", "", a).strip()
            for a in re.findall(r'<a [^>]*class="active"[^>]*>(.*?)</a>', bar, re.S)]


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
        f"/tenants/{alpha}/data/mailboxes": ("Reports", "Detail reports"),
        f"/tenants/{alpha}/evidence": ("Reports", "Questionnaires"),
        f"/tenants/{alpha}/users": ("Users", None),
        _user_href(client, alpha): ("Users", None),
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


def test_reports_row_links_each_report(client, db, seeded):
    alpha = seeded
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/evidence").get_data(as_text=True)
    row = re.search(r'<nav class="tab-pages"[^>]*>(.*?)</nav>', page, re.S).group(1)
    for href in (f"/tenants/{alpha}/data", f"/tenants/{alpha}/evidence", f"/tenants/{alpha}/report"):
        assert f'href="{href}"' in row


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
