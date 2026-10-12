"""Report tables that fit the screen: long lists in a cell read as a short summary that opens in place, columns
get room by how long their values are, and long addresses may wrap without widening their column."""

from __future__ import annotations

import copy
import re

from officesentry import coverage
from officesentry.reports.cells import wrappable
from officesentry.reports.data_reports import Many, Section
from officesentry.reports.views import _width
from tests import graphdata as gd
from tests.conftest import login, make_user
from tests.seed import seed_tenant

ROLES = [gd.GA, gd.EXCH, gd.READER, gd.GROUPS_ADMIN]


def test_long_lists_of_who_a_policy_covers_are_summarised():
    assert coverage.summarise(["bg@x.com"], [], ["Global Administrator", "Exchange Administrator"]) == ""
    assert coverage.summarise(["bg@x.com"], [], [f"Role {i}" for i in range(11)]) == "bg@x.com + 11 admin roles"
    assert coverage.summarise([], [], [f"Role {i}" for i in range(4)]) == "4 admin roles"
    assert coverage.summarise(["a", "b", "c"], ["Finance"], []) == "3 accounts + Finance"
    assert coverage.summarise([], ["G1", "G2"], ["R1", "R2"], ["All users"]) == "All users + G1, G2 + R1, R2"


def test_policy_keeps_everyone_it_names_and_a_short_form():
    d = coverage.Directory(
        groups={"s1": {"displayName": "Break glass", "members": ["u4"]}},
        user_roles={},
        role_names={r["templateId"]: r["displayName"] for r in gd.ROLE_DEFINITIONS},
        names={"u4": "bg@x.com"},
    )
    policy = copy.deepcopy(gd.CA_MFA_ALL)
    policy["conditions"]["users"] = {"includeRoles": ROLES, "excludeUsers": ["u4"], "excludeGroups": ["s1"]}
    row = coverage.describe(policy, d)
    assert row.include == [
        "Global Administrator",
        "Exchange Administrator",
        "Directory Readers",
        "Groups Administrator",
    ]
    assert row.include_summary == "4 admin roles"
    assert row.exclude == ["bg@x.com", "Break glass"] and row.exclude_summary == ""
    assert row.users == ", ".join(row.include)  # downloads and other pages still read the whole list


def test_conditional_access_report_shows_the_summary_and_opens_the_list(client, db, two_tenants):
    alpha, _ = two_tenants
    policy = copy.deepcopy(gd.CA_MFA_ALL)
    policy["conditions"]["users"] = {"includeUsers": ["All"], "excludeUsers": ["u1"], "excludeRoles": ROLES}
    seed_tenant(db, alpha, ca=[policy])
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/data/conditional-access").get_data(as_text=True)
    cell = re.search(r'<details class="more">.*?</details>', page, re.S).group(0)
    assert '<summary title="Show all 5"><span>ann@x.com + 4 admin roles</span></summary>' in cell
    assert f'<li><a href="/tenants/{alpha}/users/u1">ann@x.com</a></li>' in cell  # a person opens their page
    assert "<li>Groups Administrator</li>" in cell
    csv = client.get(f"/tenants/{alpha}/data/conditional-access.csv?section=1").get_data(as_text=True)
    assert "ann@x.com, Global Administrator, Exchange Administrator" in csv  # downloads keep every item


def test_a_list_without_a_summary_shows_two_and_how_many_more():
    assert Many(["a", "b", "c"]).short == ""
    assert Many(["a", "b", "c", "d", "e"]).short == "a, b + 3 more"
    assert Many(["a", "b", "c", "d"], "everyone").short == "everyone"
    many = Many(["a", "b", "c", "d"], "everyone")
    assert copy.deepcopy(many).parts == many.parts and copy.deepcopy(many).short == "everyone"


def test_columns_get_room_by_how_long_their_values_are():
    s = Section("T", ["A"])
    for v in ("On", "Report-only"):
        s.add([v])
    assert _width([r[0] for r in s.rows], "text") == "s"
    assert _width(["Every policy in plain words, enforced ones first, with a long reason"], "text") == "l"
    assert _width(["Global Administrator, Security Administrator"], "text") == "m"
    assert _width([Many([f"Role {i}" for i in range(11)], "11 admin roles")], "text") == "s"  # its summary shows
    assert _width(["2026-10-01"], "date") == "s"


def test_long_addresses_wrap_after_the_at_sign_only():
    assert str(wrappable("anna.kowalski@fabrikam.example")) == "anna.kowalski@<wbr>fabrikam.example"
    assert str(wrappable("ann@x.com")) == "ann@x.com"  # short ones stay whole
    assert "<wbr>" not in str(wrappable("Privileged Authentication Administrator"))
    assert str(wrappable("<b>" * 12)) == "&lt;b&gt;" * 12  # escaped


def test_table_headings_never_break_at_a_hyphen(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/data/all-users").get_data(as_text=True)
    assert "Sign\N{NON-BREAKING HYPHEN}in</a>" in page
