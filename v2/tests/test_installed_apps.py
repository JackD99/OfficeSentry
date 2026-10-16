"""Installed apps: the apps Intune found on managed devices, remote access tools first."""

from datetime import UTC, datetime

from officesentry import installed_apps as ia
from officesentry import store
from officesentry.reports import data_reports
from officesentry.snapshot import TenantSnapshot
from tests.conftest import login, make_user
from tests.seed import seed_tenant


def _report(db, tenant_id):
    return data_reports.build(db, store.get_tenant(db, tenant_id), "installed-apps", datetime.now(UTC))


def _table(report, title) -> list[dict]:
    section = next(s for s in report.sections if s.title == title)
    return [dict(zip(section.columns, row, strict=True)) for row in section.rows]


def test_remote_access_tools_are_named():
    assert ia.remote_tool("ScreenConnect Client (a1b2c3d4e5f6)") == "ScreenConnect"
    assert ia.remote_tool("TeamViewer Host") == "TeamViewer"
    assert ia.remote_tool("TightVNC") == "VNC"
    assert ia.remote_tool("GoTo Resolve Unattended") == "GoTo (LogMeIn)"
    for app in ("Google Chrome", "Microsoft Teams", "Remote Desktop Connection", None):
        assert ia.remote_tool(app) is None


def test_versions_sort_as_people_read_them():
    found = ["9.0.597.98", "141.0.7390.66", "140.0.7339.208", "141.0.7390.7"]
    assert sorted(found, key=ia.version_key, reverse=True) == [
        "141.0.7390.66",
        "141.0.7390.7",
        "140.0.7339.208",
        "9.0.597.98",
    ]


def test_report_lists_remote_tools_then_apps_by_installs(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    snap = TenantSnapshot(db, store.get_tenant(db, alpha))
    assert len(snap.objects("detected_apps", "detected_app")) == 8  # not the one without a name
    assert snap.metric("apps.remote_access") == 2

    report = _report(db, alpha)
    assert [s.title for s in report.sections] == ["Remote access tools", "Apps", "Every version"]
    remote = _table(report, "Remote access tools")
    assert [(r["Tool"], r["Devices"]) for r in remote] == [("ScreenConnect", 12), ("AnyDesk", 1)]
    apps = _table(report, "Apps")
    assert [a["App"] for a in apps[:2]] == ["Google Chrome", "Microsoft 365 Apps for business - en-gb"]
    chrome = apps[0]
    assert (chrome["Versions"], chrome["Newest version"], chrome["Devices"]) == (3, "141.0.7390.66", 12)
    assert {a["App"]: a["Platform"] for a in apps}["Microsoft Authenticator"] == "iOS"
    every = [(r["App"], r["Version"]) for r in _table(report, "Every version") if r["App"] == "Google Chrome"]
    assert [v for _, v in every] == ["141.0.7390.66", "140.0.7339.208", "9.0.597.98"]
    tiles = {t.label: t.value for t in report.tiles}
    assert tiles == {"Apps found": 6, "Remote access tools": 2}

    make_user(db, "admin")
    login(client, "admin")
    for ext in ("", ".csv", ".xlsx"):
        assert client.get(f"/tenants/{alpha}/data/installed-apps{ext}").status_code == 200


def test_without_intune_says_so(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, skus=[])
    assert _report(db, alpha).unavailable.startswith("Installed apps come from Intune")
