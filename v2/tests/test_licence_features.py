"""Licence features: what a client's licences cover, what each feature unlocks, and what they leave out."""

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from officesentry import capabilities, store
from officesentry.checks.base import RULES
from officesentry.collectors.base import REGISTRY
from officesentry.reports import data_reports
from tests.conftest import login, make_user
from tests.seed import seed_tenant

NOW = datetime.now(UTC)


def _report(db, tenant_id):
    return data_reports.build(db, store.get_tenant(db, tenant_id), "licence-features")


def _section(report, title):
    return next(s for s in report.sections if s.title == title)


def _row(section, first):
    return next(r for r in section.rows if str(r[0]) == first)


def test_it_says_what_the_licences_cover_and_what_each_one_gives(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    report = _report(db, alpha)
    features = _section(report, "What your licences cover")
    covered = {str(r[0]): str(r[1]) for r in features.rows}
    # The seeded client has Business Premium (Entra ID P1, Intune, Exchange, SharePoint) and a suspended
    # product that would have given Entra ID P2, which a suspended licence doesn't.
    assert covered == {
        "Entra ID P1": "Yes",
        "Entra ID P2": "No",
        "Intune": "Yes",
        "Defender for Office 365 P1": "No",
        "Defender for Office 365 P2": "No",
        "Exchange Online": "Yes",
        "SharePoint Online": "Yes",
    }
    p1 = _row(features, "Entra ID P1")
    assert "Microsoft 365 Business Premium" in str(p1[2]) and int(p1[3]) > 0
    assert "Sign-in activity" in str(p1[4]) and "No inactive accounts" in str(p1[5])
    assert report.highlights[0].value == "4 of 7"
    inside = _section(report, "What each licence includes")
    premium = _row(inside, "Microsoft 365 Business Premium")
    assert sorted(str(premium[3]).split(", ")) == ["Entra ID P1", "Exchange Online", "Intune", "SharePoint Online"]
    assert int(premium[1]) == 10  # purchased
    assert not any(str(r[0]) == "Power Automate Free" for r in inside.rows)  # brings none of these features


def test_it_lists_the_reports_and_checks_the_licences_leave_out(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    blocked = _section(_report(db, alpha), "What your licences leave out")
    rows = {(str(r[0]), str(r[1])) for r in blocked.rows}
    assert ("Risky users", "Report") in rows  # needs Entra ID P2, which this client doesn't have
    assert ("No accounts are at risk in Entra ID Protection", "Check") in rows
    assert all(str(r[2]) == "Entra ID P2" for r in blocked.rows)
    # Everything the client is licensed for stays out of the list.
    assert not any("Mailbox" in str(r[0]) or "Device" in str(r[0]) for r in blocked.rows)


def test_people_are_counted_from_the_licences_they_hold(db, two_tenants):
    """Ann and Bob both have Business Premium, but Bob's sign-in is blocked, so only Ann is covered."""
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    features = _section(_report(db, alpha), "What your licences cover")
    assert [int(_row(features, f)[3]) for f in ("Entra ID P1", "Intune", "Exchange Online")] == [1, 1, 1]
    assert int(_row(features, "Entra ID P2")[3]) == 0
    skus = {
        s["skuId"]: s
        for s in (store.run_objects(db, store.latest_run(db, alpha, "tenant_profile")["id"], "subscribed_sku"))
    }
    assert capabilities.held_by(["sku-bp"], skus) == {"entra_p1", "intune", "exchange", "sharepoint"}
    assert capabilities.held_by(["sku-old"], skus) == set()  # a suspended licence gives nothing
    assert capabilities.held_by(None, skus) == set()


def test_the_page_opens_for_a_client_as_well(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "someone", role="client", tenants=(alpha,))
    login(client, "someone")
    page = client.get(f"/tenants/{alpha}/data/licence-features").get_data(as_text=True)
    assert "Entra ID P1" in page and "What your licences leave out" in page
    assert "Risky users" in page


def test_without_licence_data_it_says_so(db, two_tenants):
    alpha, _ = two_tenants
    report = _report(db, alpha)
    assert report.unavailable and "hasn't been collected" in report.unavailable


# -- The map the rest of the app reads ----------------------------------------------------------


def test_every_declared_capability_is_a_real_one():
    for key, collector in REGISTRY.items():
        for flag in (*collector.needs, *collector.more_with):
            assert flag in capabilities.FLAGS, f"{key} names {flag}"
    for rule in RULES:
        for flag in rule.needs:
            assert flag in capabilities.FLAGS, f"check {rule.key} names {flag}"
    assert set(capabilities.WHAT_IT_IS) == set(capabilities.FLAGS)


@pytest.mark.parametrize("path", sorted(Path("officesentry/collectors").glob("*.py")))
def test_a_collector_that_skips_an_area_says_which_licence_it_needs(path):
    """Every capability a collector stops on is declared, so the pages can name the licence instead of
    leaving someone guessing why a report is empty."""
    source = path.read_text(encoding="utf-8")
    gated = set(re.findall(r'capabilities\.get\("([a-z0-9_]+)"\) is False', source))
    if not gated:
        return
    declared = {
        flag
        for collector in REGISTRY.values()
        if Path(collector.__module__.replace(".", "/") + ".py").name == path.name
        for flag in (*collector.needs, *collector.more_with)
    }
    assert gated <= declared, f"{path.name} stops on {sorted(gated - declared)} without declaring it"


def test_a_reports_licence_needs_come_from_the_source_its_named_for():
    assert capabilities.report_needs("mailboxes") == (("exchange",), ())
    assert capabilities.report_needs("sign-ins") == (("entra_p1",), ("entra_p2",))
    assert capabilities.report_needs("risky-users") == (("entra_p2",), ())
    assert capabilities.report_needs("devices") == (("intune",), ())
    assert capabilities.report_needs("subscriptions") == ((), ())  # licences are read whatever they are
    # A report that reads the accounts along the way isn't a licence problem because of that.
    assert capabilities.report_needs("groups") == ((), ())
    assert "mfa" not in capabilities.reports_needing("exchange")


def test_a_missing_capability_is_only_one_thats_known_to_be_missing():
    assert capabilities.missing({"intune": False}, ("intune", "exchange")) == ("intune",)
    assert capabilities.missing(None, ("intune",)) == ()  # licences not read yet: say nothing
    assert capabilities.missing({}, ("intune",)) == ()


# -- Saying so where people look ----------------------------------------------------------------


def test_a_report_the_licences_block_says_so_and_links_to_what_they_cover(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)  # Business Premium: Entra ID P1, not P2 or Defender for Office 365
    make_user(db, "admin")
    login(client, "admin")
    assert "licence-gap" not in client.get(f"/tenants/{alpha}/data/mailboxes").get_data(as_text=True)
    # A part a licence leaves out is the report's own to explain (Mail rules and protection says Safe Links
    # need Defender for Office 365), so the page doesn't add a second note.
    assert "licence-gap" not in client.get(f"/tenants/{alpha}/data/mail-protection").get_data(as_text=True)
    # Risky users already says it needs Entra ID P2, so it isn't said twice.
    assert "licence-gap" not in client.get(f"/tenants/{alpha}/data/risky-users").get_data(as_text=True)
    store.set_capabilities(db, alpha, {**store.tenant_capabilities(store.get_tenant(db, alpha)), "intune": False})
    devices = client.get(f"/tenants/{alpha}/data/devices").get_data(as_text=True)
    assert "Alpha Ltd's licences don't include Intune</strong>, which this report's data needs" in devices
    assert f"/tenants/{alpha}/data/licence-features" in devices


def test_the_report_lists_tag_what_the_licences_block(client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    tab = client.get(f"/tenants/{alpha}/data").get_data(as_text=True)
    assert "Needs Entra ID P2</span>" in tab
    assert "Needs Intune" not in tab and "Needs Exchange Online" not in tab
    picker = client.get(f"/tenants/{alpha}/data/mailboxes").get_data(as_text=True)
    assert "needs Entra ID P2</span>" in picker
    # A client whose licences haven't been read yet is told nothing about them.
    assert "Needs " not in client.get(f"/tenants/{bravo}/data").get_data(as_text=True).split("report-directory")[-1]
