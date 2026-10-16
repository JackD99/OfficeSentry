"""Odd values in addresses and forms are the request's fault: a 400 or a 404 with the portal's own page,
never a 500."""

import re

import pytest

from officesentry.db import audit
from officesentry.web import activity
from tests.conftest import login, make_user

HUGE = "9" * 25  # past SQLite's largest integer (2**63 - 1)


def _huge_url(rule) -> str:
    def value(m):
        converter, name = m.group(1), m.group(2)
        if converter == "int":
            return HUGE
        return "2026-10" if name == "period" else "x"

    return re.sub(r"<(?:(\w+)(?:\([^)]*\))?:)?(\w+)>", value, rule.rule)


def test_a_huge_id_in_any_address_is_not_found(app, client, db, two_tenants):
    make_user(db, "admin")
    login(client, "admin")
    rules = [r for r in app.url_map.iter_rules() if "<int:" in r.rule]
    assert len(rules) > 100
    for rule in rules:
        url = _huge_url(rule)
        for method in sorted(rule.methods & {"GET", "POST"}):
            resp = client.open(url, method=method)
            # 405: a rule for the other method has the same address, and werkzeug says so before the number
            assert resp.status_code in (302, 400, 403, 404, 405), (method, url, resp.status_code)


@pytest.mark.parametrize(
    "url",
    [
        f"/admin/activity?page={HUGE}",
        f"/admin/activity?tenant={HUGE}",
        f"/admin/activity.csv?tenant={HUGE}",
        f"/reporting/2026-10/progress?tenant={HUGE}",
    ],
)
def test_a_huge_number_in_a_query_is_a_bad_request(client, db, two_tenants, url):
    make_user(db, "admin")
    login(client, "admin")
    resp = client.get(url)
    assert resp.status_code == 400
    assert "A number in that request is too big." in resp.get_data(as_text=True)


def test_a_huge_number_in_a_form_is_a_bad_request(client, db, two_tenants):
    make_user(db, "admin")
    login(client, "admin")
    resp = client.post("/admin/users", data={"username": "sam@x.example", "password": "Long-enough-9!", "role": HUGE})
    assert resp.status_code == 400


@pytest.mark.parametrize("month", ["2026-09%0A", "0000-01", "2026-13"])
def test_a_sign_in_download_for_a_month_that_isnt_one_is_not_found(client, db, two_tenants, month):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    assert client.get(f"/tenants/{alpha}/activity/sign-ins-{month}.csv").status_code == 404


def test_the_activity_log_download_says_when_it_was_cut(client, db, monkeypatch):
    make_user(db, "admin")
    login(client, "admin")
    for i in range(5):
        audit(db, "admin", "report_downloaded", None, f"r{i}")
    monkeypatch.setattr(activity, "CSV_LIMIT", 3)
    lines = client.get("/admin/activity.csv").get_data(as_text=True).strip().splitlines()
    assert len(lines) == 1 + 3 + 1  # the heading, three rows and the note
    assert lines[-1].startswith("Only the newest 3 rows. Choose dates to download the rest.")
    monkeypatch.setattr(activity, "CSV_LIMIT", 100)
    assert "Only the newest" not in client.get("/admin/activity.csv").get_data(as_text=True)


def test_the_activity_log_finds_a_user_whatever_the_case(client, db):
    make_user(db, "admin")
    login(client, "admin")
    audit(db, "admin", "report_downloaded", None, "board pack")
    page = client.get("/admin/activity?user=ADMIN").get_data(as_text=True)
    assert "board pack" in page
