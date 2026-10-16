"""Mail rules and protection: mail flow rules, connectors and the anti-spam, anti-phishing, anti-malware, Safe
Links and Safe Attachments policies, read with Get- cmdlets, then the report and the email checks built on them.

Exchange is simulated with ``responses`` (``tests/mailprotectiondata.py``), through a full seeded collection.
"""

import copy
from datetime import UTC, datetime

from officesentry import mailprotection as mp
from officesentry import store
from officesentry.checks import evaluate
from officesentry.collectors import mail_protection
from officesentry.reports import data_reports, views
from officesentry.reports import mail_protection as report_module
from officesentry.snapshot import TenantSnapshot
from tests import graphdata as gd
from tests import mailprotectiondata as mpd
from tests.seed import seed_tenant
from tests.test_collectors import SKUS

NOW = datetime.now(UTC)
BUSY = (500, {"error": {"code": "InternalServerError", "message": "Try again later."}})
CHECKS = ("mail_rule_spam_bypass", "mail_rule_external_copy", "spam_allowed_domains", "spoof_protection")
CHECKS += ("malware_file_filter",)
DEFENDER_SKUS = [
    {
        **SKUS[0],
        "servicePlans": [
            *SKUS[0]["servicePlans"],
            {"servicePlanName": "ATP_ENTERPRISE", "provisioningStatus": "Success"},
        ],
    }
]
NO_EXCHANGE = [{**SKUS[0], "servicePlans": [{"servicePlanName": "AAD_PREMIUM", "provisioningStatus": "Success"}]}]


def _snap(db, tenant_id):
    return TenantSnapshot(db, store.get_tenant(db, tenant_id))


def _report(db, tenant_id):
    return data_reports.build(db, store.get_tenant(db, tenant_id), "mail-protection", now=NOW)


def _table(report, title):
    section = next(s for s in report.sections if s.title == title)
    return [dict(zip(section.columns, row, strict=True)) for row in section.rows]


def _flags(report, title):
    section = next(s for s in report.sections if s.title == title)
    return {row[0]: flag for row, flag in zip(section.rows, section.flags, strict=True)}


def _results(db, tenant_id):
    return {r.key: r for r in evaluate(_snap(db, tenant_id), NOW) if r.key in CHECKS}


def _exchange(**changes):
    data = copy.deepcopy(gd.EXCHANGE)
    data.update(changes)
    return data


def test_collects_rules_connectors_and_policies_keeping_only_what_the_report_uses(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    assert store.latest_run(db, alpha, "mail_protection")["status"] == "succeeded"
    snap = _snap(db, alpha)

    rules = {r["Name"]: r for r in mp.objects(snap, "transport_rule")}
    assert list(rules) == [r["Name"] for r in mpd.TRANSPORT_RULES]
    warning = rules["External sender warning"]
    assert len(warning["ApplyHtmlDisclaimerText"]) == mail_protection.TEXT_LIMIT
    assert not {"Identity", "ExchangeObjectId", "Conditions"} & set(warning)
    assert rules["Partner never spam"]["SenderDomainIs"] == ["partner.example"]

    connectors = {c["_id"]: c for c in mp.objects(snap, "connector")}
    assert set(connectors) == {"in:From mail gateway", "out:To mail gateway"}
    assert (
        connectors["in:From mail gateway"]["direction"] == "in" and "Identity" not in connectors["in:From mail gateway"]
    )

    policies = {p["_id"]: p for p in mp.objects(snap, "protection_policy")}
    assert set(policies) == {
        "anti_spam:Default",
        "anti_spam:Standard Preset Security Policy1702000000000",
        "anti_phish:Office365 AntiPhish Default",
        "anti_malware:Default",
        "anti_malware:Finance malware",
    }
    assert len(policies["anti_malware:Default"]["FileTypes"]) == mail_protection.LIST_LIMIT
    # Who an anti-phishing policy protects from impersonation is counted, not kept.
    assert policies["anti_phish:Office365 AntiPhish Default"]["TargetedUsersToProtect"] == 1
    finance = policies["anti_malware:Finance malware"]["rule"]
    assert finance == {"State": "Enabled", "Priority": 0, "SentToMemberOf": ["Finance"]}
    assert {p["_id"] for p in mp.objects(snap, "preset_policy")} == {
        "eop:Standard Preset Security Policy",
        "eop:Strict Preset Security Policy",
    }

    # Without Defender for Office 365, Safe Links and Safe Attachments aren't asked for.
    assert mp.parts(snap) == {
        **dict.fromkeys(("rules", "connectors", "anti_spam", "anti_phish", "anti_malware", "presets"), "read"),
        "safe_links": "unlicensed",
        "safe_attachments": "unlicensed",
    }
    asked = {r["endpoint"] for r in db.execute("SELECT endpoint FROM run_requests")}
    assert "Exchange Get-TransportRule" in asked and "Exchange Get-SafeLinksPolicy" not in asked
    assert snap.metric("mail.transport_rules") == 6
    assert snap.metric("mail.rules_skipping_spam_filter") == 2  # the disabled one doesn't count
    assert snap.metric("mail.connectors") == 2
    assert snap.metric("mail.allowed_sender_domains") == 1


def test_report_describes_each_rule_connector_and_policy_and_what_needs_a_look(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    report = _report(db, alpha)
    assert report.unavailable is None

    rules = _table(report, "Mail flow rules")
    assert [r["Rule"] for r in rules] == [r["Name"] for r in mpd.TRANSPORT_RULES]  # in priority order
    by_name = {r["Rule"]: r for r in rules}
    # List cells are text that keeps its items (data_reports.Many).
    assert by_name["Partner never spam"]["If"] == "From domains partner.example"
    assert by_name["Partner never spam"]["Then"] == "Skips spam filtering"
    assert by_name["Copy finance mail"]["Then"] == "Sends a copy to archive@outside.example"
    assert by_name["External sender warning"]["If"].parts == (
        "From outside the organisation",
        "To inside the organisation",
    )
    assert by_name["Old allow list"]["On"] == "No" and by_name["Trial redirect"]["On"] == "Testing"
    assert _flags(report, "Mail flow rules") == {
        "External sender warning": None,
        "Partner never spam": "high",  # picked out by domain, which anyone can fake
        "Copy finance mail": "high",
        "Gateway servers": "medium",  # picked out by the servers' addresses
        "Old allow list": None,
        "Trial redirect": None,  # testing, so it doesn't redirect anything yet
    }

    (inbound, outbound) = _table(report, "Connectors")
    assert inbound["Direction"] == "Incoming" and inbound["Domains"] == "Any domain"
    assert inbound["Servers"].parts == ("203.0.113.10", "203.0.113.11") and inbound["Encryption"] == "Required"
    assert outbound["Servers"] == "relay.gateway.example"
    assert outbound["Encryption"] == "TLS with a valid certificate"
    assert _flags(report, "Connectors") == {"From mail gateway": "low", "To mail gateway": None}

    policies = {(p["Kind"], p["Policy"]): p for p in _table(report, "Protection policies")}
    assert policies["Anti-spam", "Default"]["Applies to"] == "Everyone no other policy covers"
    assert policies["Anti-spam", "Standard Preset Security Policy1702000000000"]["Preset"] == "Standard"
    assert policies["Anti-malware", "Finance malware"]["Applies to"] == "Members of Finance"
    assert "Common attachments filter: on (50 file types)" in policies["Anti-malware", "Default"]["Settings"]
    section = next(s for s in report.sections if s.title == "Protection policies")
    flags = {(row[1], row[0]): flag for row, flag in zip(section.rows, section.flags, strict=True)}
    assert flags["Anti-spam", "Default"] == "medium"  # partner.example allowed
    assert flags["Anti-malware", "Default"] is None

    attention = _table(report, "Needs attention")
    assert [r["Item"] for r in attention][:2] == ["Copy finance mail", "Partner never spam"]
    assert {r["Item"] for r in attention} == {
        "Copy finance mail",
        "Partner never spam",
        "Gateway servers",
        "Default",
        "From mail gateway",
    }

    tiles = {t.label: t for t in report.tiles}
    assert tiles["Rules that skip spam filtering"].value == 2 and tiles["Rules that skip spam filtering"].tone == "bad"
    assert tiles["Rules sending mail outside"].value == 1
    view, _ = views.rows_view(report, tiles["Rules sending mail outside"].rows)  # opens just that rule
    assert [r[0] for s in views.apply(report, view).sections if s.title == "Mail flow rules" for r in s.rows] == [
        "Copy finance mail"
    ]
    assert tiles["Allowed sender domains"].value == 1
    assert tiles["Preset protection"].value == "Standard"
    assert tiles["Preset protection"].foot == "Standard for some people"
    assert tiles["Connectors"].value == 2
    assert any("Defender for Office 365" in n for n in report.notes)


def test_checks_flag_rules_that_skip_filtering_or_send_mail_out(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    results = _results(db, alpha)

    bypass = results["mail_rule_spam_bypass"]
    assert bypass.status == "fail" and bypass.summary == "2 mail flow rules skip spam filtering"
    assert [(f.subject, f.severity) for f in bypass.findings] == [
        ("Partner never spam", "high"),
        ("Gateway servers", "medium"),
    ]
    assert "From domains partner.example" in bypass.findings[0].detail

    copies = results["mail_rule_external_copy"]
    assert copies.status == "fail"
    assert [(f.subject, f.detail) for f in copies.findings] == [
        ("Copy finance mail", "Sends mail or copies to archive@outside.example")
    ]

    allowed = results["spam_allowed_domains"]
    assert allowed.status == "fail" and allowed.summary == "1 sender domain skips the spam filter"
    assert [(f.subject, f.severity) for f in allowed.findings] == [("Default", "medium")]

    assert results["spoof_protection"].status == "pass"
    assert results["malware_file_filter"].status == "pass"
    assert (
        results["malware_file_filter"].summary == "The common attachments filter is on in all 2 anti-malware policies"
    )


def test_checks_flag_spoof_protection_off_free_mail_allowed_and_no_file_filter(db, two_tenants):
    alpha, _ = two_tenants
    phish = [{**mpd.ANTI_PHISH[0], "EnableSpoofIntelligence": False}]
    spam = [{**mpd.ANTI_SPAM[0], "AllowedSenderDomains": ["gmail.com", "x.com"]}]
    malware = [{**mpd.ANTI_MALWARE[0], "EnableFileFilter": False}]
    seed_tenant(
        db,
        alpha,
        exchange=_exchange(
            **{
                "Get-AntiPhishPolicy": phish,
                "Get-HostedContentFilterPolicy": spam,
                "Get-MalwareFilterPolicy": malware,
                "Get-MalwareFilterRule": [],
                "Get-TransportRule": [],
            }
        ),
    )
    results = _results(db, alpha)
    assert results["spoof_protection"].status == "fail"
    assert results["spoof_protection"].summary == "1 anti-phishing policy weakens spoof protection"
    assert [(f.severity, f.detail) for f in results["spoof_protection"].findings] == [
        ("high", "Spoof intelligence is off")
    ]
    (finding,) = results["spam_allowed_domains"].findings
    assert finding.severity == "high" and "gmail.com, x.com are free mail or the organisation's own" in finding.detail
    assert results["malware_file_filter"].status == "fail"
    assert results["mail_rule_spam_bypass"].status == "pass"
    assert results["mail_rule_spam_bypass"].summary == "No mail flow rule skips spam filtering (0 rules checked)"

    attention = _table(_report(db, alpha), "Needs attention")
    assert [r["Kind"] for r in attention[:2]] == ["Anti-phishing policy", "Anti-spam policy"]  # high, then by kind
    assert {r["What to check"] for r in attention} >= {
        "Spoof intelligence is off, so mail pretending to be from other domains isn't caught",
        "Lets mail claiming to be from gmail.com, x.com skip spam filtering; gmail.com, x.com are free mail or the "
        "organisation's own",
        "Doesn't block common risky attachment types (.exe, .iso and the like)",
    }


def test_a_policy_with_its_rule_off_applies_to_nobody_and_isnt_flagged(db, two_tenants):
    alpha, _ = two_tenants
    malware = [*mpd.ANTI_MALWARE[:1], {**mpd.ANTI_MALWARE[1], "EnableFileFilter": False}]
    rules = [{**mpd.MALWARE_RULES[0], "State": "Disabled"}]
    seed_tenant(db, alpha, exchange=_exchange(**{"Get-MalwareFilterPolicy": malware, "Get-MalwareFilterRule": rules}))
    assert _results(db, alpha)["malware_file_filter"].status == "pass"
    policies = {
        p["Policy"]: p for p in _table(_report(db, alpha), "Protection policies") if p["Kind"] == "Anti-malware"
    }
    assert policies["Finance malware"]["Applies to"] == "Nobody (its rule is off)"


def test_with_defender_safe_links_safe_attachments_and_its_preset_are_read(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, skus=DEFENDER_SKUS, exchange=_exchange(**mpd.DEFENDER))
    snap = _snap(db, alpha)
    assert set(mp.parts(snap).values()) == {"read"}
    kinds = {p["kind"] for p in mp.objects(snap, "protection_policy")}
    assert {"safe_links", "safe_attachments"} <= kinds
    assert "defender:Standard Preset Security Policy" in {p["_id"] for p in mp.objects(snap, "preset_policy")}

    report = _report(db, alpha)
    assert not any("Defender" in n for n in report.notes)
    policies = {(p["Kind"], p["Policy"]): p for p in _table(report, "Protection policies")}
    links = policies["Safe Links", "Built-In Protection Policy"]
    assert links["Applies to"] == "Everyone no other policy covers (built-in protection)"
    assert "Email: on" in links["Settings"]
    # With Defender, an anti-phishing policy without impersonation protection is worth a look.
    assert any(r["What to check"].startswith("Impersonation protection") for r in _table(report, "Needs attention"))
    assert {t.label: t.foot for t in report.tiles}["Preset protection"] == "Standard for some people"


def test_a_part_that_fails_is_named_and_the_others_still_show(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, exchange=_exchange(**{"Get-TransportRule": BUSY, "Get-AntiPhishRule": BUSY}))
    run = store.latest_run(db, alpha, "mail_protection")
    assert run["status"] == "partial"
    snap = _snap(db, alpha)
    assert mp.parts(snap)["rules"] == "failed" and mp.parts(snap)["anti_phish"] == "failed"
    assert mp.parts(snap)["anti_spam"] == "read"

    report = _report(db, alpha)
    rules = next(s for s in report.sections if s.title == "Mail flow rules")
    assert not rules.rows and "couldn't be read" in rules.empty
    tiles = {t.label: t.value for t in report.tiles}
    assert tiles["Rules that skip spam filtering"] == "Not read" and tiles["Connectors"] == 2
    assert any("Anti-phishing policies couldn't be read" in n for n in report.notes)
    assert {p["Kind"] for p in _table(report, "Protection policies")} == {"Anti-spam", "Anti-malware"}

    results = _results(db, alpha)
    assert results["mail_rule_spam_bypass"].status == "unavailable"
    assert results["mail_rule_spam_bypass"].summary == "Mail flow rules couldn't be read; see the collection's log"
    assert results["spoof_protection"].status == "unavailable"
    assert results["spam_allowed_domains"].status == "fail"


def test_unavailable_before_collection_and_without_exchange(db, two_tenants):
    alpha, beta = two_tenants
    assert _report(db, beta).unavailable == report_module.NOT_COLLECTED
    assert {r.status for r in _results(db, beta).values()} == {"unavailable"}

    seed_tenant(db, alpha, skus=NO_EXCHANGE)
    snap = _snap(db, alpha)
    assert mp.status(snap)["available"] is False and mp.parts(snap) == {}
    assert _report(db, alpha).unavailable == report_module.NO_EXCHANGE
    results = _results(db, alpha)
    assert {r.summary for r in results.values()} == {"The tenant has no Exchange Online licence"}


def test_rule_wording_and_problems():
    gateway = {"direction": "in", "SenderDomains": ["smtp:*;1"], "EFSkipIPs": ["203.0.113.10"]}
    assert mp.connector_problems(gateway) == []
    assert mp.connector_problems({**gateway, "Enabled": False, "EFSkipIPs": []}) == []
    assert mp.domain_of("smtp:Contoso.com;1") == "contoso.com"
    assert mp.domain_of("partner.example") == "partner.example"

    rule = {"State": "Enabled", "Mode": "Enforce", "From": ["ceo@x.com"], "SetSCL": -1}
    assert mp.rule_problems(rule, {"x.com"})[0][0] == "high"
    assert mp.rule_problems({**rule, "SenderIpRanges": ["203.0.113.0/24"]}, {"x.com"})[0][0] == "medium"
    assert mp.rule_problems({**rule, "Mode": "AuditAndNotify"}, {"x.com"}) == []
    assert mp.external_copies({"RedirectMessageTo": ["Ann", "a@x.com", "b@y.example"]}, {"x.com"}) == ["b@y.example"]
    assert mp.actions({"SetSCL": 6}) == ["Marks it as spam"]
    assert mp.actions({}) == ["Nothing the report describes; see the rule's description"]

    assert mp.risky_domains(["gmail.com", "x.com", "partner.example"], {"x.com"}) == ["gmail.com", "x.com"]
    phish = {"kind": "anti_phish", "IsDefault": True, "Enabled": False, "HonorDmarcPolicy": False}
    assert mp.policy_problems(phish, {"x.com"}) == [("medium", "Ignores what senders' DMARC policies ask for")]
    spam = {"kind": "anti_spam", "IsDefault": True, "HighConfidencePhishAction": "MoveToJmf", "BulkThreshold": 9}
    assert [s for s, _ in mp.policy_problems(spam, {"x.com"})] == ["high", "low"]


def test_a_long_allow_list_is_checked_whole():
    """Every allowed sender domain is kept, so one past the first 50 is still found to be free mail."""
    allowed = [f"partner{n}.com" for n in range(60)] + ["gmail.com"]
    kept = mail_protection._kept(
        {"AllowedSenderDomains": allowed, "FileTypes": allowed}, ("AllowedSenderDomains", "FileTypes")
    )
    assert len(kept["AllowedSenderDomains"]) == 61 and len(kept["FileTypes"]) == mail_protection.LIST_LIMIT
    policy = {"kind": "anti_spam", "Name": "Default", "IsDefault": True, **kept}
    [(severity, text)] = [p for p in mp.policy_problems(policy, {"x.com"}) if "skip spam" in p[1]]
    assert severity == "high"
    assert text.startswith("Lets mail claiming to be from partner0.com, ") and "and 51 more skip spam filtering" in text
    assert text.endswith("; gmail.com is free mail or the organisation's own")
