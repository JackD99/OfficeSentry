"""Every collector, check and report against responses recorded from a real tenant.

``tests/fixtures/live_tenant.json`` comes from ``python -m officesentry live-test --record``
against a Business Premium test tenant (Entra ID P1, no P2, synced from on-premises AD)
with 11 Conditional Access policies, one excluding a group, a mail domain, a file shared
with one person, a deleted file, an inbox rule that moves mail to a folder, a mail flow
rule, Microsoft's preset protection policies and one team (names hidden in usage
reports), last on 12 Oct 2026. Names, emails,
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
from officesentry.collectors import core
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
RECORDED = datetime(2026, 10, 12, 18, 30, tzinfo=UTC)

# Requests added since the recording, answered with Microsoft's documented response shape, and requests whose
# $select asks for fields added since (the recorded response is served). Empty while the recording is current:
# add an entry when a collector starts asking for something new, and re-record soon after.
STAND_INS: dict[str, dict] = {
    # Trusted organisations and locations (collectors/policies.py, collectors/identity.py) and Installed apps
    # (collectors/detected_apps.py), in Microsoft's documented shapes: a tenant on Microsoft's defaults with one
    # named location and no organisation of its own, and two apps.
    "/v1.0/policies/crossTenantAccessPolicy/default": {
        "isServiceDefault": True,
        "inboundTrust": {
            "isMfaAccepted": False,
            "isCompliantDeviceAccepted": False,
            "isHybridAzureADJoinedDeviceAccepted": False,
        },
        "automaticUserConsentSettings": {"inboundAllowed": False, "outboundAllowed": False},
        **{
            setting: {
                "usersAndGroups": {"accessType": access, "targets": [{"target": "AllUsers", "targetType": "user"}]},
                "applications": {
                    "accessType": access,
                    "targets": [{"target": "AllApplications", "targetType": "application"}],
                },
            }
            for setting, access in (
                ("b2bCollaborationInbound", "allowed"),
                ("b2bCollaborationOutbound", "allowed"),
                ("b2bDirectConnectInbound", "blocked"),
                ("b2bDirectConnectOutbound", "blocked"),
            )
        },
    },
    "/v1.0/policies/crossTenantAccessPolicy/partners": {"value": []},
    "/v1.0/identity/conditionalAccess/namedLocations": {
        "value": [
            {
                "@odata.type": "#microsoft.graph.ipNamedLocation",
                "id": "0b5b0c1e-0000-4000-8000-000000000001",
                "displayName": "Office",
                "createdDateTime": "2026-01-05T10:00:00Z",
                "modifiedDateTime": "2026-01-05T10:00:00Z",
                "isTrusted": True,
                "ipRanges": [{"@odata.type": "#microsoft.graph.iPv4CidrRange", "cidrAddress": "198.51.100.0/24"}],
            }
        ]
    },
    "/v1.0/deviceManagement/detectedApps": {
        "value": [
            {
                "id": "0000000000000000000000000000000000000000000001",
                "displayName": "Microsoft Edge",
                "version": "141.0.3537.71",
                "sizeInByte": 0,
                "deviceCount": 1,
                "publisher": "Microsoft Corporation",
                "platform": "windows",
            },
            {
                "id": "0000000000000000000000000000000000000000000002",
                "displayName": "Microsoft 365 Apps for business - en-gb",
                "version": "16.0.19127.20264",
                "sizeInByte": 0,
                "deviceCount": 1,
                "publisher": "Microsoft Corporation",
                "platform": "windows",
            },
        ]
    },
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
    # The sign-in and audit logs (into the activity history) and the message trace are read up to "now": the
    # recording's now.
    with (
        responses.RequestsMock(assert_all_requests_are_fired=False) as rsps,
        patch("officesentry.dnscheck.lookup", replay.fake_dns(FIXTURE)),
        patch("officesentry.events.clock", lambda: RECORDED),
    ):
        missing = replay.mock(rsps, FIXTURE, STAND_INS, NEW_FIELDS)
        # The test tenant hasn't approved the SharePoint permission, so SharePoint's own API refuses the app.
        rsps.get(re.compile(r"https://[a-z0-9-]+\.sharepoint\.com(/.*)?/_api/.*"), status=401, json={})
        statuses = {}
        for key in core():  # the test tenant has no audit log add-on
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
    # and nobody has a manager set. It hasn't approved the SharePoint permission, so site permissions and the
    # owners of its sites (none has a Microsoft 365 group, so they're in SharePoint groups) wait for it, nor
    # the BitLocker permission, so that check waits too. It was recorded before the 2.4 batch, so Windows LAPS
    # and Defender alerts wait for those permissions.
    nothing_to_check = {
        "laps_backups",
        "defender_alerts",
        "site_guest_full_control",
        "site_owners",
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
    elif key == "agents":  # nor the 2.6 batch's
        assert report.unavailable.startswith("Waiting for Live test to approve") and not report.sections
    elif key in data_reports.hidden(db, live):  # nor the audit log add-on
        assert report.unavailable and not report.sections
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
    # Files saved once (Microsoft's own, in the search centre's Style Library) have no version history to list.
    history = next(s for s in report.sections if s.title == "Files with the most version history")
    assert history.rows and "0 bytes" not in [r[2] for r in history.rows]


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
    shown = [key for key in data_reports.REPORTS if key not in data_reports.hidden(db, live)]
    pages += [f"{t}/data/{key}" for key in shown] + [f"{t}/data/{key}.xlsx" for key in shown]
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
    # Recorded with the 2.2 batch approved except BitLocker and SharePoint's own permission, and before the 2.4
    # and 2.6 batches: those wait for approval, which is a step to do, not a problem.
    assert set(health.waiting) <= set(permission_batch.BATCH_OF)
    later = [p for b in permission_batch.BATCHES if b.release in ("2.4", "2.6") for p in b.permissions]
    assert sorted(health.waiting) == sorted(["BitLockerKey.ReadBasic.All", "SharePoint Sites.Read.All", *later])
    assert health.check("permissions").state in ("ok", "todo")
    assert health.onboarded
    # The replay signs in without an app connection; that is all there is to fix.
    assert [c.key for c in health.problems] == ["connection"], health.problems


def test_directory_reports_on_real_data(db, live):
    apps = data_reports.build(db, live, "unused-apps", RECORDED)
    rows = {r[0]: (r, f) for r, f in zip(apps.sections[0].rows, apps.sections[0].flags, strict=True)}
    # Office Sentry's own test app signs in every night and is left out; Entra Connect's app signed in the day
    # before; two in-house apps (one made that day) and a third party's from an unverified publisher, all
    # consented for everyone, have never signed in.
    assert len(rows) == 4
    assert sorted((r[1], r[3], f) for r, f in rows.values()) == [
        ("This organisation", "Last 30 days", None),
        ("This organisation", "No sign-in on record", "medium"),
        ("This organisation", "No sign-in on record", "medium"),
        ("Unknown publisher", "No sign-in on record", "medium"),
    ]
    groups = data_reports.build(db, live, "group-structure", RECORDED)
    tables = {s.title: s for s in groups.sections}
    assert [r[3] for r in tables["Dynamic groups"].rows] == ["Running"]
    assert tables["Empty groups"].rows and not tables["Groups inside groups"].rows
    users = data_reports.build(db, live, "all-users", RECORDED)
    assert users.unavailable is None and len(users.sections[0].rows) == 7


def _tables(report) -> dict:
    return {s.title: s for s in report.sections}


def test_teams_on_real_data(db, live):
    # One public team with only its General channel and 62 apps from the Teams store. The tenant hides names in
    # usage reports (Microsoft's default), and then the activity report hides team IDs too, so the team's
    # activity can't be told: it mustn't be listed as inactive for that.
    report = data_reports.build(db, live, "teams", RECORDED)
    tables = _tables(report)
    assert len(tables["Every team"].rows) == 1 and tables["Every team"].rows[0][1:4] == ["Public", 1, 0]
    assert not tables["Inactive teams"].rows and tables["Inactive teams"].empty.startswith("Microsoft hides team")
    assert len(tables["Apps in teams"].rows) == 62 and {r[2] for r in tables["Apps in teams"].rows} == {"Teams store"}
    tiles = {t.label: t.value for t in report.tiles}
    assert tiles["Inactive teams"] == "Hidden" and tiles["Custom uploaded apps"] == 0
    assert "hiding people's names" in report.notice


def test_site_access_on_real_data(db, live):
    # One site of the tenant's own, with no Microsoft 365 group; Microsoft's search centre (/search) and content
    # type hub (/sites/contentTypeHub, named "Team Site") are left out. SharePoint lists both "Everyone" claims in
    # the user list though nothing grants them, and the site's administrators are the Global and SharePoint
    # Administrator roles, not people. Its owners group needs SharePoint's own permission, which the tenant hasn't
    # approved.
    sites = TenantSnapshot(db, live).objects("site_access", "site")
    assert [s["url"] for s in sites] == ["https://sentrytest3.sharepoint.com/"] and not sites[0]["group"]
    assert all(s["everyone"] is None and s["everyoneKnown"] is False for s in sites)
    assert all(s["ownerCount"] is None and not s["admins"] for s in sites)
    report = data_reports.build(db, live, "site-access", RECORDED)
    tiles = {t.label: (t.value, t.foot) for t in report.tiles}
    assert tiles["Sites with no owner"][0] == 0 and tiles["Sites"] == (1, "Owners unknown for 1 site")
    assert tiles["Open to everyone in the organisation"] == (0, "Not known for 1 site yet")
    every = _tables(report)["Who can access each site"]
    assert {r[6] for r in every.rows} == {"Unknown"}


def test_mail_protection_on_real_data(db, live):
    # Defender for Office 365's Standard and Strict presets are on (Business Premium has them), there's one mail
    # flow rule (a signature service's, on automatic replies) and no connectors.
    report = data_reports.build(db, live, "mail-protection", RECORDED)
    tiles = {t.label: t.value for t in report.tiles}
    assert tiles["Preset protection"] == "Standard and Strict" and tiles["Connectors"] == 0
    assert tiles["Rules that skip spam filtering"] == tiles["Rules sending mail outside"] == 0
    tables = _tables(report)
    assert len(tables["Mail flow rules"].rows) == 1 and tables["Mail flow rules"].rows[0][3] == "Message type OOF"
    presets = {r[3] for r in tables["Protection policies"].rows}
    assert presets == {"", "Standard", "Strict"}
    results = _results(db, live)
    for key in ("mail_rule_spam_bypass", "mail_rule_external_copy", "spam_allowed_domains", "malware_file_filter"):
        assert results[key].status == "pass", key
    assert results["spoof_protection"].status == "pass"


def test_mail_flow_on_real_data(db, live):
    # Ten days of the message trace, read a day at a time: 21 messages, most of them received, and Microsoft's
    # own summary of who got the most spam.
    report = data_reports.build(db, live, "mail-flow", RECORDED)
    tables = _tables(report)
    per_domain = tables["Mail per domain"].rows
    assert sum(r[1] + r[2] + r[3] for r in per_domain) == 21
    assert sum(r[4] + r[5] + r[6] + r[7] for r in per_domain) == 21  # delivered, junk, quarantined, failed
    assert len(tables["Top spam recipients"].rows) == 1 and not tables["Top phishing recipients"].rows


def test_intune_policies_and_pim_on_real_data(db, live):
    # Intune's policy lists all answer (the tenant has none yet), and no group holds an admin role, so there's
    # nobody who could activate one through PIM for groups.
    snap = TenantSnapshot(db, live)
    (status,) = snap.objects("intune_policies", "intune_policy_status")
    assert status["available"] and set(status["parts"].values()) == {"read"} and status["waiting"] == []
    report = data_reports.build(db, live, "intune-policies", RECORDED)
    assert report.unavailable is None and not any(s.rows for s in report.sections)
    assert snap.objects("admin_roles", "pim_groups_status")[0]["state"] == "none"
    assert snap.objects("devices", "bitlocker_status")[0]["state"] == "waiting"
