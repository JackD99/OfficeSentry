"""Every collector, check and report against responses recorded from a real tenant.

``tests/fixtures/live_tenant.json`` comes from ``python -m officesentry live-test --record``
against a Business Premium test tenant (Entra ID P1, no P2) with 11 Conditional Access
policies. Names, emails, phone numbers, domains and IDs are replaced; the response
shapes are Microsoft's. Re-record when a collector starts asking for something new.
"""

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
from officesentry.reports import data_reports, evidence, export
from officesentry.reports.monthly_review import build
from officesentry.rules import evaluate
from officesentry.snapshot import TenantSnapshot
from officesentry.worker import execute_run
from tests import replay
from tests.conftest import login, make_user

FIXTURE = replay.load()
# When the recording was made; checks that depend on dates (inactive accounts) use it.
RECORDED = datetime(2026, 10, 3, 17, 15, tzinfo=UTC)

# Requests added since the recording, answered with Microsoft's documented response shape (here, a new
# tenant's default policy). Delete an entry when the fixture is re-recorded; the test below says when.
STAND_INS = {
    "/beta/auditLogs/signIns": {
        "@odata.context": "https://graph.microsoft.com/beta/$metadata#auditLogs/signIns", "value": []},
    "/v1.0/policies/authenticationMethodsPolicy": {
        "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#policies/authenticationMethodsPolicy",
        "id": "authenticationMethodsPolicy", "displayName": "Authentication Methods Policy",
        "description": "The tenant-wide policy that controls which authentication methods are allowed in the "
                       "tenant, authentication method registration requirements, and self-service password reset "
                       "settings",
        "lastModifiedDateTime": "2026-03-22T18:43:07Z", "policyVersion": "1.5", "reconfirmationInDays": 0,
        "policyMigrationState": "migrationComplete",
        "registrationEnforcement": {"authenticationMethodsRegistrationCampaign": {
            "snoozeDurationInDays": 1, "enforceRegistrationAfterAllowedSnoozes": True, "state": "default",
            "excludeTargets": [], "includeTargets": [{"id": "all_users", "targetType": "group",
                                                      "targetedAuthenticationMethod": "microsoftAuthenticator"}]}},
        "reportSuspiciousActivitySettings": {"state": "default", "voiceReportingCode": 0,
                                             "includeTarget": {"targetType": "group", "id": "all_users"}},
        "systemCredentialPreferences": {"state": "default", "excludeTargets": [],
                                        "includeTargets": [{"id": "all_users", "targetType": "group"}]},
        "authenticationMethodConfigurations": [
            {"@odata.type": "#microsoft.graph.fido2AuthenticationMethodConfiguration", "id": "Fido2",
             "state": "enabled", "isSelfServiceRegistrationAllowed": True, "isAttestationEnforced": False,
             "keyRestrictions": {"isEnforced": False, "enforcementType": "block", "aaGuids": []},
             "includeTargets": [{"targetType": "group", "id": "all_users", "isRegistrationRequired": False}],
             "excludeTargets": []},
            {"@odata.type": "#microsoft.graph.microsoftAuthenticatorAuthenticationMethodConfiguration",
             "id": "MicrosoftAuthenticator", "state": "enabled", "isSoftwareOathEnabled": False,
             "featureSettings": {
                 "displayAppInformationRequiredState": {
                     "state": "default", "includeTarget": {"targetType": "group", "id": "all_users"},
                     "excludeTarget": {"targetType": "group", "id": "00000000-0000-0000-0000-000000000000"}},
                 "displayLocationInformationRequiredState": {
                     "state": "default", "includeTarget": {"targetType": "group", "id": "all_users"},
                     "excludeTarget": {"targetType": "group", "id": "00000000-0000-0000-0000-000000000000"}}},
             "includeTargets": [{"targetType": "group", "id": "all_users", "isRegistrationRequired": False,
                                 "authenticationMode": "any"}], "excludeTargets": []},
            {"@odata.type": "#microsoft.graph.smsAuthenticationMethodConfiguration", "id": "Sms", "state": "disabled",
             "includeTargets": [], "excludeTargets": []},
            {"@odata.type": "#microsoft.graph.voiceAuthenticationMethodConfiguration", "id": "Voice",
             "state": "disabled", "isOfficePhoneAllowed": False, "includeTargets": [], "excludeTargets": []},
            {"@odata.type": "#microsoft.graph.emailAuthenticationMethodConfiguration", "id": "Email",
             "state": "enabled", "allowExternalIdToUseEmailOtp": "default", "excludeTargets": [],
             "includeTargets": [{"targetType": "group", "id": "all_users", "isRegistrationRequired": False}]},
            {"@odata.type": "#microsoft.graph.softwareOathAuthenticationMethodConfiguration", "id": "SoftwareOath",
             "state": "disabled", "includeTargets": [], "excludeTargets": []},
            {"@odata.type": "#microsoft.graph.temporaryAccessPassAuthenticationMethodConfiguration",
             "id": "TemporaryAccessPass", "state": "disabled", "defaultLifetimeInMinutes": 60,
             "defaultLength": 8, "minimumLifetimeInMinutes": 60, "maximumLifetimeInMinutes": 480,
             "isUsableOnce": False, "includeTargets": [], "excludeTargets": []},
            {"@odata.type": "#microsoft.graph.x509CertificateAuthenticationMethodConfiguration",
             "id": "X509Certificate", "state": "disabled", "includeTargets": [], "excludeTargets": []},
        ],
    },
}
# Requests whose $select asks for fields added since the recording; the recorded response is served.
NEW_FIELDS = frozenset({"/v1.0/domains"})


# Replaying every collector takes a few seconds, and the tests below only read what it stored, so the
# whole module shares one database (``cfg`` and ``db`` override conftest's per-test ones here).
@pytest.fixture(scope="module")
def cfg(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        for var in ("SECRET_KEY", "ENCRYPTION_KEYS", "DATA_DIR"):
            mp.delenv("OFFICESENTRY_" + var, raising=False)
        return load_config(data_dir=tmp_path_factory.mktemp("live") / "data", testing=True, secure_cookies=False,
                           require_mfa="off", extra={"disable_csrf": True})


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
    graph = lambda t, c: GraphClient(lambda: "t", sleep=lambda s: None)  # noqa: E731
    exo = lambda t, c: ExchangeClient(t["tenant_ref"], lambda: "t", sleep=lambda s: None)  # noqa: E731
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps, \
            patch("officesentry.dnscheck.lookup", replay.fake_dns(FIXTURE)):
        missing = replay.mock(rsps, FIXTURE, STAND_INS, NEW_FIELDS)
        statuses = {}
        for key in REGISTRY:
            store.enqueue_run(db, tenant_id, key)
            statuses[key] = execute_run(db, store.claim_next_run(db), graph, exo, client_id=FIXTURE["client_id"])
    assert not missing, f"requests not in the recording: {missing}"
    assert {k for k, v in statuses.items() if v != "succeeded"} == set(), statuses
    return store.get_tenant(db, tenant_id)


def test_stand_ins_are_only_for_requests_not_recorded():
    """Once the fixture is re-recorded with these requests, delete them from STAND_INS and NEW_FIELDS."""
    recorded = [replay.request_key(e["url"]) for e in FIXTURE["entries"] if e["api"] == "graph"]
    paths = {path for path, _ in recorded}
    assert not set(STAND_INS) & paths
    for path in NEW_FIELDS:
        selects = [dict(q).get("$select", "") for p, q in recorded if p == path]
        assert selects and all("passwordValidityPeriodInDays" not in s for s in selects), path


def _results(db, tenant):
    return {r.key: r for r in evaluate(TenantSnapshot(db, tenant), RECORDED)}


def test_every_check_runs_on_real_data(db, live):
    results = _results(db, live)
    # The test tenant has no Intune devices and no MTA-STS, so those checks have nothing to read, and no
    # Entra ID P2, so sign-in risk is hidden.
    nothing_to_check = {"tls_rpt", "device_compliance", "device_encryption", "supported_windows", "intune_enrolled",
                        "device_checkin", "risky_sign_ins"}
    assert {k for k, r in results.items() if r.status == "unavailable"} == nothing_to_check
    assert results["legacy_auth_blocked"].status == "pass"
    assert results["admins_ca_mfa"].status == "pass"


def test_own_and_microsoft_apps_are_not_flagged(db, live):
    apps = _results(db, live)["risky_app_permissions"]
    names = {f.subject for f in apps.findings}
    assert len(names) == 1  # only the tenant's own automation app with mail access
    assert "Microsoft Graph Command Line Tools" not in names


def test_empty_registration_report_falls_back_to_per_user_methods(db, live):
    # On a newly licensed tenant userRegistrationDetails returns [], as recorded.
    rows = TenantSnapshot(db, live).objects("mfa_registration", "mfa_registration")
    assert rows and {r["source"] for r in rows} == {"per_user_methods"}
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
    policies = {row[0]: (row, flag) for row, flag in zip(report.sections[0].rows, report.sections[0].flags,
                                                          strict=True)}
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


def test_first_collection_is_a_baseline(db, live):
    report = build(db, live)
    assert report.first_report and report.new_count == 0 and report.failing


@pytest.mark.parametrize("key", list(data_reports.REPORTS))
def test_detail_reports_and_exports(db, live, key):
    report = data_reports.build(db, live, key, RECORDED)
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
    assert evidence.to_xlsx(evidence.build(db, live))[:2] == b"PK"


def test_pages_and_downloads(client, db, live):
    make_user(db, "boss", all_tenants=True)
    login(client, "boss")
    t = f"/tenants/{live['id']}"
    pages = ["/", t, f"{t}/report", f"{t}/evidence", f"{t}/findings", f"{t}/collection", f"{t}/schedules",
             f"{t}/report.xlsx", f"{t}/evidence.xlsx"]
    pages += [f"{t}/data/{key}" for key in data_reports.REPORTS] + \
        [f"{t}/data/{key}.xlsx" for key in data_reports.REPORTS]
    if export.pdf_available():
        pages += [f"{t}/report.pdf", f"{t}/evidence.pdf"]
    for url in pages:
        assert client.get(url).status_code == 200, url
    page = client.get(f"{t}/report").get_data(as_text=True)
    assert "first report for this tenant" in page


def test_usage_reports_on_real_data(db, live):
    # The recording's usage reports have one Business Standard user who only receives mail.
    snap = TenantSnapshot(db, live)
    people = [r for r in snap.objects("m365_usage", "user_usage") if r["products"]]
    assert len(people) == 2 and all(r["platforms"] is not None for r in people)
    report = data_reports.build(db, live, "licence-rightsizing", RECORDED)
    [move] = report.sections[0].rows
    assert move[1] == "Microsoft 365 Business Standard" and move[3] == "Convert to a shared mailbox"
    usage = data_reports.build(db, live, "usage", RECORDED)
    assert not usage.unavailable and usage.sections[0].rows[0][:2] == ["Email", 1]


def test_sign_in_methods_on_real_data(db, live):
    # The registration report was empty, so methods were read one person at a time.
    report = data_reports.build(db, live, "auth-methods", RECORDED)
    sections = {s.title: s for s in report.sections}
    assert any("one person at a time" in n for n in report.notes)
    strongest = {row[0]: row[1] for row in sections["Strongest method"].rows}
    assert strongest["Phishing-resistant"] == 1 and strongest["None"] == 3
    strengths = {row[0][:5]: row[3] for row in sections["Authentication strengths in Conditional Access"].rows}
    assert strengths["CA001"] == "Yes" and strengths["CA002"] == "No"
    results = _results(db, live)
    admins = results["admin_phishing_resistant"]
    assert admins.status == "fail" and len(admins.findings) == 1
    assert results["phone_only_mfa"].status == "pass"


def test_subscriptions_on_real_data(db, live):
    # Business Premium (no Teams), Power Automate Free and a lapsed Business Standard trial. The subscriptions
    # behind them and the users' licence assignment states were added by hand (see those entries' "note").
    report = data_reports.build(db, live, "subscriptions", RECORDED)
    renewals = next(s for s in report.sections if s.title == "Renewals and expiry")
    assert (str(renewals.rows[0][0]), renewals.rows[0][1]) == ("Microsoft 365 Business Standard", "Suspended")
    who = next(s for s in report.sections if s.title == "Who has which licence")
    assert who.rows and {r[2] for r in who.rows} == {"Directly"}
    results = _results(db, live)
    assert results["licence_assignment_errors"].status == "pass"
    assert results["subscriptions_lapsing"].status == "pass"  # the lapsed trial has nobody on it


def test_connection_health_on_real_data(db, live):
    # The test app holds every permission Office Sentry asks for, and Global Reader.
    from officesentry import tenant_health

    health = tenant_health.assess(db, live)
    result = health.result
    assert result["app_found"] and result["global_reader"] and not result["missing"]
    assert "Exchange.ManageAsAppV2" in result["other"]  # granted, but not one Office Sentry asks for
    assert health.onboarded
    # The replay signs in without an app connection; that is all there is to fix.
    assert [c.key for c in health.problems] == ["connection"], health.problems
