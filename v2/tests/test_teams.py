"""Teams: channels, the guests and people from other organisations in private and shared channels, apps added
to teams, inactive teams, and waiting (not failing) while the client hasn't approved the 2.2 permissions.

Graph is simulated with ``responses`` (``tests/teamsdata.py``), through a full seeded collection.
"""

from datetime import UTC, datetime

import responses

from officesentry import store
from officesentry.reports import data_reports
from officesentry.snapshot import TenantSnapshot
from officesentry.usage import COLLECTOR
from officesentry.worker import execute_run
from tests import teamsdata
from tests.seed import graph_factory, mock_tenant, seed_tenant

NOW = datetime.now(UTC)
CHANNEL_PERMS = ("Channel.ReadBasic.All", "ChannelMember.Read.All", "TeamsAppInstallation.ReadForTeam.All")


def _report(db, tenant_id):
    return data_reports.build(db, store.get_tenant(db, tenant_id), "teams", now=NOW)


def _table(report, title):
    section = next(s for s in report.sections if s.title == title)
    return [dict(zip(section.columns, row, strict=True)) for row in section.rows]


def _run(db, tenant_id, collector="teams"):
    return store.latest_run(db, tenant_id, collector)


def test_teams_report_lists_channels_outsiders_apps_and_inactive_teams(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    assert _run(db, alpha)["status"] == "succeeded"
    report = _report(db, alpha)
    assert report.unavailable is None and report.notice == ""

    every = {r["Team"]: r for r in _table(report, "Every team")}
    assert set(every) == {"Sales", "Project Phoenix", "Board"}  # Microsoft 365 groups with a team only
    assert every["Project Phoenix"]["Channels"] == 3 and every["Project Phoenix"]["Shared channels"] == 1
    assert every["Project Phoenix"]["Guests in channels"] == 1
    assert every["Project Phoenix"]["From other organisations"] == 1
    assert every["Sales"]["Apps"] == 2 and every["Sales"]["Last activity"] == teamsdata.ACTIVITY[0]["lastActivityDate"]

    channels = {r["Channel"]: r for r in _table(report, "Private and shared channels")}
    assert set(channels) == {"Deals", "Supplier"}  # standard channels have the team's own members
    assert channels["Deals"]["Type"] == "Private" and channels["Deals"]["Members"] == 2
    assert channels["Supplier"]["Owners"] == 1

    people = {r["Person"]: r["Who"] for r in _table(report, "Guests and people from other organisations")}
    assert people == {"Gail Guest": "Guest", "Pat Partner": "From another organisation"}

    apps = _table(report, "Apps in teams")
    assert {(a["Team"], a["App"], a["Source"]) for a in apps} >= {
        ("Sales", "Expenses bot", "Custom upload"),
        ("Project Phoenix", "Contoso CRM", "Your organisation"),
    }
    inactive = _table(report, "Inactive teams")
    assert [(r["Team"], r["Last activity"]) for r in inactive] == [("Board", "No activity recorded")]
    tiles = {t.label: t.value for t in report.tiles}
    assert tiles == {
        "Teams": 3,
        "Inactive teams": 1,
        "Private and shared channels": 2,
        "Guests and outsiders in channels": 2,
        "Custom uploaded apps": 1,
    }


def test_a_client_that_has_not_approved_yet_gets_a_waiting_report_not_a_failed_run(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, our_app={"missing": CHANNEL_PERMS})
    run = _run(db, alpha)
    assert run["status"] == "succeeded"  # waiting isn't partial
    calls = db.execute("SELECT endpoint FROM run_requests WHERE run_id = ?", (run["id"],)).fetchall()
    assert not [c for c in calls if "/teams/" in c["endpoint"]]  # nothing asked of Teams while waiting

    report = _report(db, alpha)
    assert report.notice.startswith("Waiting for Alpha Ltd to approve the new read-only permissions this needs")
    for p in CHANNEL_PERMS:
        assert p in report.notice
    every = {r["Team"]: r for r in _table(report, "Every team")}
    assert (
        every["Sales"]["Channels"] == ""
        and every["Sales"]["Last activity"] == teamsdata.ACTIVITY[0]["lastActivityDate"]
    )
    assert [r["Team"] for r in _table(report, "Inactive teams")] == ["Board"]  # activity still reads
    tiles = {t.label: (t.value, t.foot) for t in report.tiles}
    assert tiles["Private and shared channels"] == ("Waiting", "For the client's approval")
    section = next(s for s in report.sections if s.title == "Apps in teams")
    assert section.empty == "The apps in each team will show once the client approves the new permission."


def test_a_refused_new_permission_counts_as_waiting_before_the_connection_check_says_anything(db, two_tenants):
    alpha, _ = two_tenants
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        mock_tenant(rsps, teams_kw={"status": 403})
        store.enqueue_run(db, alpha, "teams")  # no connection check yet
        execute_run(db, store.claim_next_run(db), graph_factory)
    run = _run(db, alpha)
    assert run["status"] == "succeeded"
    status = TenantSnapshot(db, store.get_tenant(db, alpha)).objects("teams", "teams_status")[0]
    assert status["parts"] == {"channels": "waiting", "members": "read", "apps": "waiting"}
    report = _report(db, alpha)
    assert {t.label: t.value for t in report.tiles}["Custom uploaded apps"] == "Waiting"


def test_a_refused_permission_the_connection_check_says_is_granted_is_a_failure_not_waiting(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, teams_kw={"status": 403})  # the connection check finds every permission granted
    run = _run(db, alpha)
    assert run["status"] == "partial" and "Teams channels weren't readable" in run["error"]
    status = TenantSnapshot(db, store.get_tenant(db, alpha)).objects("teams", "teams_status")[0]
    assert status["parts"] == {"channels": "failed", "members": "read", "apps": "failed"}
    assert status["waiting"] == []
    report = _report(db, alpha)
    assert report.notice == ""
    tiles = {t.label: (t.value, t.foot) for t in report.tiles}
    assert tiles["Custom uploaded apps"] == ("Not read", "See the collection's log")


def test_report_settings_say_outright_when_names_are_concealed(db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha, usage_kw={"concealed": True})
    seed_tenant(db, bravo, our_app={"missing": ("ReportSettings.Read.All",)})
    status = TenantSnapshot(db, store.get_tenant(db, alpha)).objects(COLLECTOR, "usage_status")[0]
    assert status["concealSetting"] is True and status["concealed"] is True
    waiting = TenantSnapshot(db, store.get_tenant(db, bravo)).objects(COLLECTOR, "usage_status")[0]
    assert waiting["concealSetting"] is None  # not asked while waiting; the codes still tell


def test_hidden_names_dont_make_every_team_look_inactive(db, two_tenants):
    # With names hidden, Microsoft's activity report gives every team the same all-zero ID, so no row matches
    # a team. That isn't "no activity": the report says it can't tell rather than list every team.
    alpha, _ = two_tenants
    seed_tenant(db, alpha, teams_kw={"activity": teamsdata.CONCEALED_ACTIVITY})
    report = _report(db, alpha)
    assert not _table(report, "Inactive teams")
    inactive = next(s for s in report.sections if s.title == "Inactive teams")
    assert inactive.empty.startswith("Microsoft hides team names in this tenant's usage reports")
    tiles = {t.label: (t.value, t.foot, t.tone) for t in report.tiles}
    assert tiles["Inactive teams"] == ("Hidden", "Microsoft hides team names", None)
    assert {r["Last activity"] for r in _table(report, "Every team")} == {""}
    assert "Microsoft is hiding people's names" in report.notice
