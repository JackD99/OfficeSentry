"""Every collector, check and report against responses recorded from a real tenant.

``tests/fixtures/live_tenant.json`` comes from ``python -m officesentry live-test --record``
against a Business Premium (no Teams) test tenant (Entra ID P1, no P2, synced from
on-premises AD) with 11 Conditional Access policies, one excluding a group, a mail domain,
a file shared with one person, a deleted file and an inbox rule that moves mail to a
folder, last on 10 Oct 2026. Names, emails,
phone numbers, domains and IDs are replaced; the response shapes are Microsoft's.
Re-record when a collector starts asking for something new (DNS too: run it with
``--dns-over-https`` where plain DNS is blocked).
"""

import re
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
import responses

from officesentry import coverage, store
from officesentry.collectors import REGISTRY
from officesentry.config import load_config
from officesentry.db import connect, migrate
from officesentry.exchange import ExchangeClient
from officesentry.graph import GraphClient
from officesentry.reports import data_reports, evidence, export, scheme_export, schemes
from officesentry.reports.monthly_review import build
from officesentry.rules import evaluate
from officesentry.snapshot import TenantSnapshot
from officesentry.worker import execute_run
from tests import replay
from tests.conftest import login, make_user

FIXTURE = replay.load()
# When the recording was made; checks that depend on dates (inactive accounts) use it.
RECORDED = datetime(2026, 10, 10, 19, 30, tzinfo=UTC)

# Requests added since the recording, answered with Microsoft's documented response shape, and requests whose
# $select asks for fields added since (the recorded response is served). Empty while the recording is current:
# add an entry when a collector starts asking for something new, and re-record soon after.
STAND_INS: dict[str, dict] = {
    # Site access (collectors/site_access.py): each site's hidden user list and its default library's
    # permissions, in Microsoft's documented shapes.
    "/v1.0/sites/{id}/lists/User%20Information%20List/items": {
        "value": [
            {
                "id": "1",
                "contentType": {"name": "Person"},
                "fields": {
                    "Title": "Site Admin",
                    "EMail": "admin@contoso.example",
                    "Name": "i:0#.f|membership|admin@contoso.example",
                    "IsSiteAdmin": True,
                    "Deleted": False,
                },
            }
        ]
    },
    "/v1.0/sites/{id}/drive/root/permissions": {
        "value": [
            {"id": "1", "roles": ["owner"], "grantedToV2": {"siteGroup": {"displayName": "Site Owners", "id": "3"}}}
        ]
    },
    # The recording's Secure Score page carries a next link that the collector didn't follow when it was
    # made (it read one page); now it does, and the page behind the link is answered empty until the next
    # recording has it.
    "/v1.0/security/secureScores" + replay.PAGED: {"value": []},
    # Administrative units (the test tenant has none).
    "/v1.0/directory/administrativeUnits": {"value": []},
    "/v1.0/directory/administrativeUnits/{id}/members": {"value": []},
    # The Teams collector's activity report (2.2); the test tenant has no Teams, so Microsoft lists no teams.
    "/beta/reports/getTeamsTeamActivityDetail(period='D90')": {"value": []},
    # Mail flow (2.2): the whole message trace a day at a time, and Microsoft's top spam and phishing recipients.
    "Get-MessageTraceV2(EndDate,ResultSize,StartDate)": {"value": []},
    "Get-MailTrafficSummaryReport": {"value": []},
    # Mail rules and protection (2.3): a tenant as Microsoft sets it up, with no rules or connectors, the
    # default policies, and Defender for Office 365's built-in protection (Business Premium has it).
    "Get-TransportRule": {"value": []},
    "Get-InboundConnector": {"value": []},
    "Get-OutboundConnector": {"value": []},
    "Get-HostedContentFilterPolicy": {
        "value": [
            {
                "Name": "Default",
                "IsDefault": True,
                "BulkThreshold": 7,
                "SpamAction": "MoveToJmf",
                "HighConfidenceSpamAction": "MoveToJmf",
                "PhishSpamAction": "MoveToJmf",
                "HighConfidencePhishAction": "Quarantine",
                "BulkSpamAction": "MoveToJmf",
                "AllowedSenderDomains": [],
                "SpamZapEnabled": True,
                "PhishZapEnabled": True,
            }
        ]
    },
    "Get-HostedContentFilterRule": {"value": []},
    "Get-AntiPhishPolicy": {
        "value": [
            {
                "Name": "Office365 AntiPhish Default",
                "IsDefault": True,
                "Enabled": True,
                "EnableSpoofIntelligence": True,
                "HonorDmarcPolicy": True,
                "EnableUnauthenticatedSender": True,
                "EnableMailboxIntelligenceProtection": False,
                "EnableTargetedUserProtection": False,
            }
        ]
    },
    "Get-AntiPhishRule": {"value": []},
    "Get-MalwareFilterPolicy": {
        "value": [{"Name": "Default", "IsDefault": True, "EnableFileFilter": True, "FileTypes": ["exe", "iso"]}]
    },
    "Get-MalwareFilterRule": {"value": []},
    "Get-SafeLinksPolicy": {
        "value": [{"Name": "Built-In Protection Policy", "IsBuiltInProtection": True, "EnableSafeLinksForEmail": True}]
    },
    "Get-SafeLinksRule": {"value": []},
    "Get-SafeAttachmentPolicy": {"value": [{"Name": "Built-In Protection Policy", "IsBuiltInProtection": True}]},
    "Get-SafeAttachmentRule": {"value": []},
    "Get-EOPProtectionPolicyRule": {"value": []},
    "Get-ATPProtectionPolicyRule": {"value": []},
}
NEW_FIELDS: frozenset[str] = frozenset()


# Replaying every collector takes a few seconds, and the tests below only read what it stored, so the
# whole module shares one database (``cfg`` and ``db`` override conftest's per-test ones here).
@pytest.fixture(scope="module")
def cfg(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        for var in ("SECRET_KEY", "ENCRYPTION_KEYS", "DATA_DIR"):
            mp.delenv("OFFICESENTRY_" + var, raising=False)
        return load_config(
            data_dir=tmp_path_factory.mktemp("live") / "data",
            testing=True,
            secure_cookies=False,
            require_mfa="off",
            extra={"disable_csrf": True},
        )


@pytest.fixture(scope="module")
def db(cfg):
    conn = connect(cfg.db_path)
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def live(db):
    tenant_id = store.create_tenant(db, "Live test", FIXTURE["tenant_id"], None)
    db.execute("INSERT INTO settings (key, value) VALUES ('live_test_client_id', ?)", (FIXTURE["client_id"],))
    graph = lambda t, c: GraphClient(lambda: "t", sleep=lambda s: None, resource_tokens=lambda h: lambda: "t")  # noqa: E731
    exo = lambda t, c: ExchangeClient(t["tenant_ref"], lambda: "t", sleep=lambda s: None)  # noqa: E731
    # The sign-in and audit logs are read up to "now" into the activity history: the recording's now.
    with (
        responses.RequestsMock(assert_all_requests_are_fired=False) as rsps,
        patch("officesentry.dnscheck.lookup", replay.fake_dns(FIXTURE)),
        patch("officesentry.events.clock", lambda: RECORDED),
    ):
        missing = replay.mock(rsps, FIXTURE, STAND_INS, NEW_FIELDS)
        # The test tenant hasn't approved the SharePoint permission, so SharePoint's own API refuses the app.
        rsps.get(re.compile(r"https://[a-z0-9-]+\.sharepoint\.com(/.*)?/_api/.*"), status=401, json={})
        statuses = {}
        for key in REGISTRY:
            store.enqueue_run(db, tenant_id, key)
            statuses[key] = execute_run(db, store.claim_next_run(db), graph, exo, client_id=FIXTURE["client_id"])
    assert not missing, f"requests not in the recording: {missing}"
    assert {k for k, v in statuses.items() if v != "succeeded"} == set(), statuses
    return store.get_tenant(db, tenant_id)


def test_stand_ins_are_only_for_requests_not_recorded():
    """Once the fixture is re-recorded with these requests, delete them from STAND_INS and NEW_FIELDS."""
    recorded = set()
    for e in FIXTURE["entries"]:
        if e["api"] == "graph":
            path, query = replay.request_key(e["url"])
            recorded.add(path + replay.PAGED if any(k == "$skiptoken" for k, _ in query) else path)
        elif e["api"] == "exchange":
            name = e["cmdlet"]["CmdletName"]
            recorded.add(name)
            recorded.add(f"{name}({','.join(sorted(e['cmdlet'].get('Parameters') or {}))})")
    assert not set(STAND_INS) & recorded
    assert not any(re.fullmatch(path.replace("{id}", "[^/]+"), r) for path in STAND_INS for r in recorded)


def _results(db, tenant):
    return {r.key: r for r in evaluate(TenantSnapshot(db, tenant), RECORDED)}


def test_every_check_runs_on_real_data(db, live):
    results = _results(db, live)
    # The test tenant has no Intune devices and no MTA-STS, so those checks have nothing to read, and no
    # Entra ID P2, so sign-in risk and risky users are hidden. Its one Windows PC is only Entra-registered,
    # and nobody has a manager set. It hasn't approved the SharePoint permission, so site permissions wait
    # for it, nor the 2.2 Intune and BitLocker permissions, so their checks wait too.
    nothing_to_check = {
        "site_guest_full_control",
        "bitlocker_keys",
        "intune_compliance_coverage",
        "tls_rpt",
        "blocked_manager",
        "device_compliance",
        "device_encryption",
        "intune_enrolled",
        "device_checkin",
        "risky_sign_ins",
        "risky_users",
    }
    assert {k for k, r in results.items() if r.status == "unavailable"} == nothing_to_check
    assert results["legacy_auth_blocked"].status == "pass"
    assert results["admins_ca_mfa"].status == "pass"
    assert results["supported_windows"].status == "fail"  # Windows 11 24H2, whose support ends on 13 Oct 2026


def test_own_and_microsoft_apps_are_not_flagged(db, live):
    apps = _results(db, live)["risky_app_permissions"]
    names = {f.subject for f in apps.findings}
    assert len(names) == 1  # only the tenant's own automation app with mail access
    assert "Microsoft Graph Command Line Tools" not in names


def test_registration_report_on_real_data(db, live):
    # The registration report now has a row per user (on 3 Oct it was still empty on this newly licensed
    # tenant; the per-user fallback for that is covered with simulated data in test_identity_collectors.py).
    rows = TenantSnapshot(db, live).objects("mfa_registration", "mfa_registration")
    assert rows and {r["source"] for r in rows} == {"registration_report"}
    assert sum(r["isMfaRegistered"] for r in rows) == 2
    results = _results(db, live)
    assert results["admin_mfa"].status == "pass"
    assert results["users_mfa_registered"].status == "fail"
    assert TenantSnapshot(db, live).metric("mfa.users_checked") == len(rows)


def test_risk_based_policy_isnt_mfa_for_everyone(db, live):
    result = _results(db, live)["mfa_enforced"]
    assert "CA010" not in result.summary and "CA002" in result.summary
    assert len(result.findings) == 1  # the break-glass account, counted once
    cov = coverage.user_coverage(TenantSnapshot(db, live))
    assert not any("CA010" in p for c in cov for p in c.mfa_policies)


def test_p2_conditions_and_authentication_strength_are_described(db, live):
    report = data_reports.build(db, live, "conditional-access", RECORDED)
    policies = {
        row[0]: (row, flag) for row, flag in zip(report.sections[0].rows, report.sections[0].flags, strict=True)
    }
    ca010 = next(v for k, v in policies.items() if k.startswith("CA010"))
    ca002 = next(v for k, v in policies.items() if k.startswith("CA002"))
    ca001 = next(v for k, v in policies.items() if k.startswith("CA001"))
    assert "needs Entra ID P2" in ca010[0][1] and ca010[1] == "medium"
    assert "P2" not in ca002[0][1] and ca002[1] is None
    assert ca001[0][6] == "Phishing-resistant MFA"


def test_delegated_access_leaves_out_exchange_system_entries(db, live):
    report = data_reports.build(db, live, "mailbox-permissions", RECORDED)
    access = next(s for s in report.sections if s.title == "Every permission")
    text = str([s.rows for s in report.sections])
    assert access.rows and "NULL SID" not in text and "Discovery" not in text and "ExchangeOnline" not in text


def test_inbox_rule_folder_names_on_real_data(db, live):
    # Graph refuses to name the folder (no mail access); Get-InboxRule names it, matched by rule identity.
    report = data_reports.build(db, live, "inbox-rules", RECORDED)
    rules = next(s for s in report.sections if s.title == "All inbox rules")
    assert [r[4] for r in rules.rows] == ["move to Quarantine; stop processing rules"]


def test_conditional_access_exclusion_group_on_real_data(db, live):
    snap = TenantSnapshot(db, live)
    assert [g["members"] for g in snap.objects("conditional_access", "ca_group")] == [[]]  # an empty group
    policies = next(
        s for s in data_reports.build(db, live, "conditional-access", RECORDED).sections if s.title == "Policies"
    )
    assert any(g["displayName"] in r[3] for r in policies.rows for g in snap.objects("conditional_access", "ca_group"))


def test_first_collection_is_a_baseline(db, live):
    report = build(db, live)
    assert report.first_report and report.new_count == 0 and report.failing


@pytest.mark.parametrize("key", list(data_reports.REPORTS))
def test_detail_reports_and_exports(db, live, key):
    report = data_reports.build(db, live, key, RECORDED)
    if key == "risky-users":  # the test tenant has no Entra ID P2
        assert "Entra ID P2" in report.unavailable and not report.sections
    elif key == "site-permissions":  # nor the SharePoint permission
        assert report.unavailable.startswith("Waiting for the client to approve") and not report.sections
    else:
        assert report.unavailable is None and report.sections
    assert data_reports.to_xlsx(report)[:2] == b"PK"


def test_sharepoint_storage_on_real_data(db, live):
    report = data_reports.build(db, live, "sharepoint-storage", RECORDED)
    libraries = next(s for s in report.sections if s.title == "Libraries")
    # Real libraries, read through SharePoint's storage figures; system lists like Events are left out.
    assert libraries.rows and not any("Events" in str(r[0]) for r in libraries.rows)
    assert {t.label for t in report.tiles} >= {"Storage used", "Older versions", "Recycle bins"}
    assert "(estimated)" in report.tiles[0].foot


def test_monthly_review_and_evidence_export(db, live):
    assert export.to_xlsx(build(db, live))[:2] == b"PK"
    assert evidence.build(db, live).controls
    for key in schemes.SCHEMES:
        assert scheme_export.to_xlsx(schemes.assess(db, live, key))[:2] == b"PK"


def test_pages_and_downloads(client, db, live):
    make_user(db, "boss", all_tenants=True)
    login(client, "boss")
    t = f"/tenants/{live['id']}"
    pages = [
        "/",
        t,
        f"{t}/report",
        f"{t}/evidence",
        f"{t}/findings",
        f"{t}/collection",
        f"{t}/schedules",
        f"{t}/report.xlsx",
        f"{t}/evidence/cyber-essentials",
        f"{t}/evidence/insurance.xlsx",
        f"{t}/evidence/supplier.docx",
        f"{t}/evidence/supplier.txt",
    ]
    pages += [f"{t}/data/{key}" for key in data_reports.REPORTS] + [
        f"{t}/data/{key}.xlsx" for key in data_reports.REPORTS
    ]
    if export.pdf_available():
        pages += [f"{t}/report.pdf", f"{t}/evidence/insurance.pdf", f"{t}/evidence/cyber-essentials.pdf"]
    for url in pages:
        assert client.get(url).status_code == 200, url
    page = client.get(f"{t}/report").get_data(as_text=True)
    # The first report: nothing to compare with yet, but admin changes from the activity history are listed
    # (an app secret added on 6 Oct 2026) while the report's period still reaches back to the recording.
    changes = page.split('id="changes"')[1].split("</section>")[0]
    assert "first report for your organisation" in page or "Admin activity" in changes
    assert client.get(f"{t}/activity").status_code == 200


def test_usage_reports_on_real_data(db, live):
    # One Business Premium user who receives mail and uses Excel and PowerPoint on Windows, and one with only
    # Power Automate Free: nobody to move to a cheaper licence.
    snap = TenantSnapshot(db, live)
    people = [r for r in snap.objects("m365_usage", "user_usage") if r["products"]]
    assert len(people) == 2 and all(r["platforms"] is not None for r in people)
    report = data_reports.build(db, live, "licence-rightsizing", RECORDED)
    assert not report.unavailable and report.sections[0].rows == []
    usage = data_reports.build(db, live, "usage", RECORDED)
    assert not usage.unavailable and usage.sections[0].rows[0][:2] == ["Email", 1]
    office = next(s for s in usage.sections if s.title == "Office apps by platform")
    assert {r[0] for r in office.rows} == {"Excel", "PowerPoint"}


def test_sign_in_methods_on_real_data(db, live):
    report = data_reports.build(db, live, "auth-methods", RECORDED)
    sections = {s.title: s for s in report.sections}
    assert not any("one person at a time" in n for n in report.notes)  # the registration report had everyone
    strongest = {row[0]: row[1] for row in sections["Strongest method"].rows}
    assert strongest["Phishing-resistant"] == 1 and strongest["None"] == 3
    # Every method in Microsoft's real policy has a name, and Microsoft's newest method values are understood.
    policy = [str(row[0]) for row in sections["Sign-in methods policy"].rows]
    assert "Verified ID (Face Check)" in policy and "VerifiableCredentials" not in policy
    registered = {str(row[0]): row[1] for row in sections["Methods registered"].rows}
    assert registered["Synced passkey"] == "Phishing-resistant"
    strengths = {row[0][:5]: row[3] for row in sections["Authentication strengths in Conditional Access"].rows}
    assert strengths["CA001"] == "Yes" and strengths["CA002"] == "No"
    results = _results(db, live)
    admins = results["admin_phishing_resistant"]
    assert admins.status == "fail" and len(admins.findings) == 1
    assert results["phone_only_mfa"].status == "pass"


def test_subscriptions_on_real_data(db, live):
    # Business Premium (no Teams), Power Automate Free and a suspended Business Standard trial.
    report = data_reports.build(db, live, "subscriptions", RECORDED)
    renewals = next(s for s in report.sections if s.title == "Renewals and expiry")
    assert (str(renewals.rows[0][0]), renewals.rows[0][2]) == ("Microsoft 365 Business Standard", "Suspended")
    who = next(s for s in report.sections if s.title == "Who has which licence")
    assert who.rows and {r[2] for r in who.rows} == {"Directly"}
    results = _results(db, live)
    assert results["licence_assignment_errors"].status == "pass"
    assert results["subscriptions_lapsing"].status == "pass"  # the lapsed trial has nobody on it


def test_connection_health_on_real_data(db, live):
    # The test app holds every permission Office Sentry asked for when it was recorded, and Global Reader.
    from officesentry import permission_batch, tenant_health

    health = tenant_health.assess(db, live)
    result = health.result
    assert result["app_found"] and result["global_reader"] and not health.missing
    assert "Exchange.ManageAsAppV2" in result["other"]  # granted, but not one Office Sentry asks for
    # Recorded before the 2.2 batch: those wait for approval, which is a step to do, not a problem.
    assert set(health.waiting) <= set(permission_batch.CURRENT.permissions)
    assert health.check("permissions").state in ("ok", "todo")
    assert health.onboarded
    # The replay signs in without an app connection; that is all there is to fix.
    assert [c.key for c in health.problems] == ["connection"], health.problems


def test_directory_reports_on_real_data(db, live):
    apps = data_reports.build(db, live, "unused-apps", RECORDED)
    rows = {r[0]: (r, f) for r, f in zip(apps.sections[0].rows, apps.sections[0].flags, strict=True)}
    # Office Sentry's own test app signs in every night and is left out; Entra Connect's app signed in the day
    # before; an in-house app consented for everyone has never signed in.
    assert len(rows) == 2 and all(r[1] == "This organisation" for r, _ in rows.values())
    assert sorted((r[3], f) for r, f in rows.values()) == [("Last 30 days", None), ("No sign-in on record", "medium")]
    groups = data_reports.build(db, live, "group-structure", RECORDED)
    tables = {s.title: s for s in groups.sections}
    assert [r[3] for r in tables["Dynamic groups"].rows] == ["Running"]
    assert tables["Empty groups"].rows and not tables["Groups inside groups"].rows
    users = data_reports.build(db, live, "all-users", RECORDED)
    assert users.unavailable is None and len(users.sections[0].rows) == 7
