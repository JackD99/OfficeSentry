"""Outlook and mail apps: collected from Microsoft's email apps report, with Outlook versions told apart by support.
Graph is simulated at the HTTP level (``tests.seed``, ``tests.usagedata``)."""

from datetime import UTC, date, datetime

from officesentry import store, usage
from officesentry.collectors.usage import merge_mail_apps
from officesentry.reports import data_reports
from officesentry.reports.mail_apps import NOT_READ, NOT_YET, _lifecycle
from officesentry.snapshot import TenantSnapshot
from tests import usagedata as ud
from tests.conftest import login, make_user
from tests.seed import seed_tenant


def _report(db, tenant_id, now=None):
    return data_reports.build(db, store.get_tenant(db, tenant_id), "mail-apps", now or datetime.now(UTC))


def _section(report, title):
    return next(s for s in report.sections if s.title == title)


def _table(report, title) -> dict[str, dict]:
    section = _section(report, title)
    return {str(row[0]): dict(zip(section.columns, row, strict=True)) for row in section.rows}


def test_collector_keeps_each_persons_mail_apps(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    snap = TenantSnapshot(db, store.get_tenant(db, alpha))
    assert usage.status(snap)["read"]["mail_apps"] is True
    ann = usage.person(snap, "ann@x.com")["mailApps"]
    assert ann["imap"] == ["Undetermined"] and ann["web"] == ["Undetermined"] and ann["windows"] == []
    assert usage.person(snap, "carol@x.com")["mailApps"]["windows"] == ["Outlook 2016"]
    assert not any(usage.person(snap, "dan@x.com")["mailApps"].values())  # listed, but used nothing
    assert usage.person(snap, "bob@x.com")["mailApps"] is None  # not in the report
    run = store.latest_run(db, alpha, "m365_usage")
    assert run["status"] == "succeeded"
    asked = {r["endpoint"] for r in db.execute("SELECT endpoint FROM run_requests WHERE run_id = ?", (run["id"],))}
    assert "beta /reports/getEmailAppUsageUserDetail(period={period})" in asked


def test_versions_come_as_a_list_or_as_text():
    rows = {"ann@x.com": {"mailApps": None}}
    merge_mail_apps(
        rows,
        [
            {"userPrincipalName": "Ann@x.com", "outlookForWindows": "Outlook 2016; Outlook M365", "pop3App": ["", 3]},
            {"userPrincipalName": "gone@x.com", "outlookForWindows": ["Outlook 2010"]},  # not in active users
        ],
    )
    apps = rows["ann@x.com"]["mailApps"]
    assert apps["windows"] == ["Outlook 2016", "Outlook M365"] and apps["pop"] == [] and apps["smtp"] == []


def test_support_is_told_by_the_year_in_the_name():
    assert usage.outlook_support_ends("Outlook 2016") == date(2025, 10, 14)
    assert usage.outlook_support_ends("Outlook 2024") == date(2029, 10, 9)
    assert usage.outlook_support_ends("Outlook M365") is None
    assert usage.outlook_support_ends("Undetermined") is None
    row = {"mailApps": {"windows": ["Outlook 2021", "Outlook M365"], "mac": ["Outlook 2016"]}}
    assert usage.old_outlook(row, date(2026, 10, 1)) == ["Outlook 2016"]
    assert usage.old_outlook(row, date(2026, 10, 13)) == ["Outlook 2016", "Outlook 2021"]
    assert _lifecycle(date(2026, 10, 24)) == (
        "Outlook 2021 reached the end of support on 13 Oct 2026, and Outlook 2024 reaches it on 9 Oct 2029"
    )
    assert _lifecycle(date(2030, 1, 1)) == "Outlook 2024 reached the end of support on 9 Oct 2029"


def test_mail_apps_report(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    report = _report(db, alpha)
    assert report.unavailable is None
    assert [s.title for s in report.sections] == [
        "Outlook versions",
        "People on an unsupported Outlook",
        "POP, IMAP and SMTP apps",
        "Everyone's mail apps",
    ]
    versions = _section(report, "Outlook versions")
    assert versions.rows == [
        ["Outlook 2016", "Windows", 1, "Ended 14 Oct 2025"],
        ["Outlook M365", "Windows", 1, "Supported, kept up to date"],
        ["Version not known", "Mac", 1, "Not known"],
    ]
    assert versions.flags == ["high", None, None]

    old = _table(report, "People on an unsupported Outlook")
    assert list(old) == ["carol@x.com"]
    carol = old["carol@x.com"]
    assert carol["Outlook"].parts == ("Outlook 2016",) and carol["Support ended"] == "14 Oct 2025"
    assert carol["Licence includes desktop Outlook"] == "Yes"  # Business Standard

    protocols = _table(report, "POP, IMAP and SMTP apps")
    assert protocols["ann@x.com"]["Uses"].parts == ("IMAP",)
    assert protocols["ann@x.com"]["Also uses"].parts == ("Outlook on the web", "Other mobile mail apps")
    assert protocols["erin@x.com"]["Uses"].parts == ("SMTP",)
    assert _section(report, "POP, IMAP and SMTP apps").flags == ["medium", "medium"]

    everyone = _table(report, "Everyone's mail apps")
    assert list(everyone) == ["ann@x.com", "carol@x.com", "erin@x.com"]
    assert everyone["erin@x.com"]["Outlook for Mac"].parts == ("Version not known",)
    assert everyone["erin@x.com"]["Outlook mobile"] == "Yes" and everyone["ann@x.com"]["Outlook mobile"] == ""

    tiles = {t.label: (t.value, t.foot, t.tone) for t in report.tiles}
    assert tiles["Unsupported Outlook"][0::2] == (1, "bad")
    assert tiles["POP, IMAP or SMTP"][0::2] == (2, "warn")
    assert tiles["Desktop Outlook"][:2] == (2, "1 on Microsoft 365's, kept up to date")
    assert tiles["Mail app users"][0] == 3

    make_user(db, "admin")
    login(client, "admin")
    for ext in ("", ".csv", ".xlsx"):
        assert client.get(f"/tenants/{alpha}/data/mail-apps{ext}").status_code == 200


def test_a_version_turns_unsupported_on_its_day(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    before = _report(db, alpha, datetime(2025, 10, 1, tzinfo=UTC))
    assert _section(before, "Outlook versions").rows[0] == ["Outlook 2016", "Windows", 1, "Supported until 14 Oct 2025"]
    assert not _section(before, "People on an unsupported Outlook").rows
    assert {t.label: t.tone for t in before.tiles}["Unsupported Outlook"] == "ok"
    assert (
        "Outlook 2013 reached the end of support on 11 Apr 2023, and Outlook 2016 and 2019 reach it on 14 Oct 2025."
        in before.footnote
    )


def test_when_the_report_fails_it_says_so(db, two_tenants, monkeypatch):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha, usage_kw={"mail_status": 500})
    assert store.latest_run(db, alpha, "m365_usage")["status"] == "partial"
    assert _report(db, alpha).unavailable == NOT_READ
    # The other usage reports don't mention it.
    usage_report = data_reports.build(db, store.get_tenant(db, alpha), "usage", datetime.now(UTC))
    assert not any("email apps" in n for n in usage_report.notes)

    # Usage collected before the email apps report was read.
    seed_tenant(db, bravo)
    old = {**usage.status(TenantSnapshot(db, store.get_tenant(db, bravo))), "read": {"email": True}}
    monkeypatch.setattr(usage, "status", lambda snap: old)
    assert _report(db, bravo).unavailable == NOT_YET


def test_tenants_only_see_their_own_mail_apps(db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    seed_tenant(db, bravo, usage_kw={"mail_apps": ud.MAIL_APPS[:2]})
    assert len(_table(_report(db, alpha), "Everyone's mail apps")) == 3
    assert list(_table(_report(db, bravo), "Everyone's mail apps")) == ["ann@x.com"]
    assert not _section(_report(db, bravo), "People on an unsupported Outlook").rows
