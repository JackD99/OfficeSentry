"""Every download is named the same way (``tables.filename``): the report's key, the table's name only when
the report has several, then the client, or ``all-clients`` (``clients-<group>`` with a group chosen), and the
name is quoted in the header whatever the format."""

from __future__ import annotations

from datetime import date

import pytest

from officesentry import tenant_groups
from officesentry.reports import data_reports, export, tables
from officesentry.reports.data_reports import Section
from tests.conftest import login, make_user
from tests.seed import seed_tenant

needs_pdf = pytest.mark.skipif(not export.pdf_available(), reason="WeasyPrint not installed")


def _name(resp) -> str:
    assert resp.status_code == 200
    header = resp.headers["Content-Disposition"]
    assert header.startswith('attachment; filename="') and header.endswith('"'), header
    return header[len('attachment; filename="') : -1]


@pytest.fixture
def reports(monkeypatch):
    """Two test reports: Widgets with two tables, Gadgets with one."""

    def widgets(snap, now, report):
        report.sections = [
            Section("Widgets", ["Widget", "State"], rows=[["w1", "ok"]], flags=[None]),
            Section("Settings", ["Setting", "Value"], rows=[["x", "y"]], flags=[None]),
        ]

    def gadgets(snap, now, report):
        report.sections = [Section("Gadgets", ["Gadget"], rows=[["g1"]], flags=[None])]

    monkeypatch.setitem(data_reports.REPORTS, "widgets", ("Widgets", "Test widgets", (), widgets))
    monkeypatch.setitem(data_reports.REPORTS, "gadgets", ("Gadgets", "Test gadgets", (), gadgets))


def test_filename_joins_the_parts_it_is_given_and_names_the_day():
    assert tables.filename("admins", "Alpha Ltd") == "admins-alpha-ltd"
    assert tables.filename("guests", "", "Alpha Ltd") == "guests-alpha-ltd"
    assert tables.filename("guests", "Guest accounts", "Alpha & Co", on=date(2026, 9, 1)) == (
        "guests-guest-accounts-alpha-co-2026-09-01"
    )
    assert tables.filename("") == "report"


def test_a_clients_downloads_name_the_report_the_table_when_there_are_several_and_the_client(
    client, db, two_tenants, reports
):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    assert _name(client.get(f"/tenants/{alpha}/data/widgets.xlsx")) == "widgets-alpha-ltd.xlsx"
    assert _name(client.get(f"/tenants/{alpha}/data/widgets.csv?section=1")) == "widgets-widgets-alpha-ltd.csv"
    assert _name(client.get(f"/tenants/{alpha}/data/widgets.csv?section=2")) == "widgets-settings-alpha-ltd.csv"
    # A report with one table doesn't repeat itself.
    assert _name(client.get(f"/tenants/{alpha}/data/gadgets.csv")) == "gadgets-alpha-ltd.csv"
    assert _name(client.get(f"/tenants/{alpha}/data/gadgets.xlsx")) == "gadgets-alpha-ltd.xlsx"
    assert _name(client.get(f"/tenants/{alpha}/changes.csv")).startswith("changes-")
    assert _name(client.get(f"/tenants/{alpha}/changes.xlsx")).endswith("-alpha-ltd.xlsx")


@needs_pdf
def test_a_clients_pdf_is_named_like_its_workbook(client, db, two_tenants, reports):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    assert _name(client.get(f"/tenants/{alpha}/data/gadgets.pdf")) == "gadgets-alpha-ltd.pdf"


def test_findings_downloads_name_the_table_only_on_the_page_with_several(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    assert _name(client.get(f"/tenants/{alpha}/findings.xlsx")) == "findings-alpha-ltd.xlsx"
    assert _name(client.get(f"/tenants/{alpha}/findings.csv?section=1")) == "findings-open-findings-alpha-ltd.csv"
    assert _name(client.get(f"/tenants/{alpha}/findings.csv?section=2")) == "findings-accepted-risks-alpha-ltd.csv"
    assert _name(client.get(f"/tenants/{alpha}/findings/resolved.xlsx")) == "findings-resolved-alpha-ltd.xlsx"
    assert _name(client.get(f"/tenants/{alpha}/findings/resolved.csv")) == "findings-resolved-alpha-ltd.csv"


def test_cross_client_downloads_say_all_clients_or_the_group(client, db, two_tenants, reports):
    alpha, bravo = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    assert _name(client.get("/across/widgets.csv")) == "all-clients-widgets.csv"
    assert _name(client.get("/across/widgets.xlsx")) == "all-clients-widgets.xlsx"
    assert _name(client.get("/across/widgets/rows/widgets.csv")) == "all-clients-widgets-widgets.csv"
    assert _name(client.get("/across/widgets/rows/settings.xlsx")) == "all-clients-widgets-settings.xlsx"
    assert _name(client.get("/across/checks.csv")) == "all-clients-checks.csv"
    assert _name(client.get("/changes.csv")) == "all-clients-changes-30-days.csv"
    assert _name(client.get("/changes.xlsx?days=90")) == "all-clients-changes-90-days.xlsx"
    assert _name(client.get("/upcoming.csv")) == "coming-up.csv"

    gold = tenant_groups.create(db, {"name": "Gold Tier", "category": "Tier", "description": ""}, "test")
    tenant_groups.set_members(db, gold, [alpha])
    assert client.get(f"/across/widgets?group={gold}").status_code == 302
    assert _name(client.get("/across/widgets.csv")) == "clients-gold-tier-widgets.csv"
    assert _name(client.get("/across/widgets/rows/widgets.csv")) == "clients-gold-tier-widgets-widgets.csv"
    assert _name(client.get("/across/checks.xlsx")) == "clients-gold-tier-checks.xlsx"
    assert _name(client.get("/changes.csv")) == "clients-gold-tier-changes-30-days.csv"
