"""One page per account: what it brings together, links to it from reports, access and history."""

from datetime import UTC, datetime
from io import BytesIO

from openpyxl import load_workbook

from officesentry import store
from officesentry.reports import data_reports, user_profile
from officesentry.reports.data_reports import UserLink
from tests import graphdata as gd
from tests.conftest import login, make_user
from tests.seed import seed_tenant
from tests.test_collectors import USERS

ANN = USERS[0]


def _seed(db, tenant_id, **kwargs):
    seed_tenant(db, tenant_id, users=[*USERS, gd.INFO_USER], **kwargs)


def _tenant(db, tenant_id):
    return store.get_tenant(db, tenant_id)


def test_profile_brings_the_account_together(db, two_tenants):
    alpha, _ = two_tenants
    _seed(db, alpha)
    profile = user_profile.build(db, _tenant(db, alpha), "u1")
    assert profile.name == "Ann" and profile.upn == "ann@x.com"
    parts = {p.key: p for p in profile.parts}
    assert list(parts) == ["licences", "usage", "mfa", "roles", "groups", "mailbox", "devices", "onedrive", "apps",
                           "activity"]
    assert parts["licences"].tables[0].rows[0][0] == "Microsoft 365 Business Premium"
    groups = {row[0]: row[2] for row in parts["groups"].tables[0].rows}
    assert groups["Sales"] == "Owner"  # an owner of the team
    assert any(role == "Member" for role in groups.values())
    devices = [row[0] for row in parts["devices"].tables[0].rows]
    assert len(devices) == 2  # Ann's two managed devices, not Bob's
    # Ann has full access to the info@ mailbox: it shows under the mailboxes she can open.
    mailbox_tables = {t.title: t for t in parts["mailbox"].tables}
    can_open = mailbox_tables["Mailboxes this person can open or send from"]
    assert any(str(row[0]) == "info@x.com" for row in can_open.rows)
    # ...and links to that mailbox account's own page.
    link = next(row[0] for row in can_open.rows if str(row[0]) == "info@x.com")
    assert isinstance(link, UserLink) and link.page_href == f"/tenants/{alpha}/users/u7"
    assert profile.tiles[0].label == "Needs attention"
    assert profile.history_since


def test_disabled_account_with_licences_is_flagged(db, two_tenants):
    alpha, _ = two_tenants
    _seed(db, alpha)
    profile = user_profile.build(db, _tenant(db, alpha), "u2")
    texts = [a.text for a in profile.attention]
    assert any("Sign-in is blocked but the account still holds 1 paid licence" in t for t in texts)
    assert ("Sign-in blocked", "bad") in profile.badges


def test_unknown_account_is_none(db, two_tenants):
    alpha, _ = two_tenants
    _seed(db, alpha)
    assert user_profile.build(db, _tenant(db, alpha), "nobody") is None


def test_history_spots_licence_and_sign_in_changes(db, two_tenants):
    alpha, _ = two_tenants
    _seed(db, alpha)
    changed = [dict(USERS[0], accountEnabled=False, assignedLicenses=[]), *USERS[1:], gd.INFO_USER]
    seed_tenant(db, alpha, users=changed)
    profile = user_profile.build(db, _tenant(db, alpha), "u1")
    texts = [(e.area, e.text) for e in profile.history]
    assert ("Account", "Sign-in blocked") in texts
    assert ("Licences", "Microsoft 365 Business Premium removed") in texts


def test_report_cells_link_to_the_account_page(db, two_tenants):
    alpha, _ = two_tenants
    _seed(db, alpha)
    report = data_reports.build(db, _tenant(db, alpha), "mfa")
    cells = [v for s in report.sections for row in s.rows for v in row if isinstance(v, UserLink)]
    assert cells and all(c.page_href.startswith(f"/tenants/{alpha}/users/") for c in cells)
    # Downloads keep the plain name.
    assert all(c.safe_href is None for c in cells)
    assert cells[0] == str(cells[0])  # still the name for comparisons, sorting and exports


def test_pages_render_and_are_tenant_scoped(client, db, two_tenants):
    alpha, bravo = two_tenants
    _seed(db, alpha)
    make_user(db, "admin")
    make_user(db, "client", role="client", tenants=(bravo,))
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/users")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert f"/tenants/{alpha}/users/u1" in html and "Every account" in html
    page = client.get(f"/tenants/{alpha}/users/u1")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    for heading in ("Needs attention", "Licences", "MFA and sign-in protection", "Teams and groups", "Mailbox",
                    "Devices", "OneDrive", "History"):
        assert heading in html, heading
    assert client.get(f"/tenants/{alpha}/users/nobody").status_code == 404
    xlsx = client.get(f"/tenants/{alpha}/users/u1.xlsx")
    assert xlsx.status_code == 200
    sheets = load_workbook(BytesIO(xlsx.data)).sheetnames
    assert "Needs attention" in sheets and "Account" in sheets
    assert client.get(f"/tenants/{alpha}/users/u1.pdf").status_code == 200
    # Detail reports link account names to their page.
    assert f'href="/tenants/{alpha}/users/u1"' in client.get(f"/tenants/{alpha}/data/mfa").get_data(as_text=True)
    # Findings that name an account link to its page.
    assert f"/tenants/{alpha}/users/" in client.get(f"/tenants/{alpha}/findings").get_data(as_text=True)

    client.post("/logout")
    login(client, "client")
    assert client.get(f"/tenants/{alpha}/users/u1").status_code == 404
    assert client.get(f"/tenants/{alpha}/users").status_code == 404


def test_directory_counts(db, two_tenants):
    alpha, _ = two_tenants
    _seed(db, alpha)
    rows, tiles = user_profile.directory(db, _tenant(db, alpha), now=datetime(2026, 10, 2, tzinfo=UTC))
    assert {r.id for r in rows} == {"u1", "u2", "u3", "u7"}
    assert {t.label: t.value for t in tiles}["Accounts"] == 4
    bob = next(r for r in rows if r.id == "u2")
    assert bob.mfa == "–" and not bob.enabled


def test_usage_shows_on_the_account_page(db, two_tenants):
    alpha, _ = two_tenants
    _seed(db, alpha)
    profile = user_profile.build(db, _tenant(db, alpha), "u1")
    usage = {p.key: p for p in profile.parts}["usage"]
    assert not usage.unavailable
    labels = [label for label, _ in usage.facts]
    assert "Email" in labels and "Teams" in labels
