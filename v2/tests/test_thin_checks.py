"""The Accounts, Guests and Storage checks, and DKIM for domains behind a mail gateway."""

import copy

from officesentry import dnscheck, store
from officesentry.checks.groups import owner_is_guest
from officesentry.rules import evaluate
from officesentry.snapshot import TenantSnapshot
from tests import graphdata as gd
from tests import groupdata as grp
from tests import sharepoint_data
from tests.seed import seed_tenant
from tests.test_collectors import USERS
from tests.test_domains import resolver

GB = 1024**3


def results(db, tenant_id):
    return {r.key: r for r in evaluate(TenantSnapshot(db, store.get_tenant(db, tenant_id)))}


# -- Accounts ----------------------------------------------------------------------------------------


def test_an_account_reporting_to_a_blocked_manager_is_found(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    r = results(db, alpha)["blocked_manager"]
    assert r.status == "fail" and r.summary == "1 account reports to a blocked manager"
    assert [(f.subject, f.severity, f.object_id) for f in r.findings] == [("ann@x.com", "low", "u1")]
    assert r.findings[0].detail == "Reports to Bob, whose sign-in is blocked"


def test_blocked_manager_passes_and_is_unavailable_without_managers(db, two_tenants):
    alpha, bravo = two_tenants
    users = copy.deepcopy(USERS)
    next(u for u in users if u["id"] == "u2")["accountEnabled"] = True
    seed_tenant(db, alpha, users=users)
    r = results(db, alpha)["blocked_manager"]
    assert r.status == "pass" and r.summary == "Every manager set on 1 account can sign in"
    db.execute("DELETE FROM runs WHERE collector = 'user_details' AND tenant_id = ?", (alpha,))
    assert "managers haven't been collected" in results(db, alpha)["blocked_manager"].summary
    for u in users:
        u.pop("manager", None)
    seed_tenant(db, bravo, users=users)
    r = results(db, bravo)["blocked_manager"]
    assert r.status == "unavailable" and r.summary == "No account has a manager set"


# -- Guests ------------------------------------------------------------------------------------------


def test_a_guest_who_owns_a_team_is_found(db, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    pat = grp._user("gx1", "Pat Supplier", "pat_supplier.com#EXT#@x.onmicrosoft.com", user_type="Guest")
    monkeypatch.setitem(grp.OWNERS, "g4", [grp.ANN, pat])
    seed_tenant(db, alpha)
    r = results(db, alpha)["guest_owners"]
    assert r.status == "fail" and r.summary == "1 team or group with a guest as owner"
    assert [(f.subject, f.severity, f.object_id) for f in r.findings] == [("Board", "medium", "g4")]
    assert r.findings[0].detail == "Team, owned by Pat Supplier (guest)"
    snap = TenantSnapshot(db, store.get_tenant(db, alpha))
    board = next(g for g in snap.objects("groups", "group") if g["_id"] == "g4")
    assert [o["guest"] for o in board["owners"]] == [False, True]


def test_no_guest_owners_passes_and_old_snapshots_go_by_the_sign_in_name(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    r = results(db, alpha)["guest_owners"]
    assert r.status == "pass" and r.summary == "No guest owns any of 5 teams and groups"
    assert owner_is_guest({"name": "Pat", "upn": "pat_supplier.com#EXT#@x.onmicrosoft.com", "type": "user"})
    assert not owner_is_guest({"name": "Ann", "upn": "ann@x.com", "type": "user"})
    assert not owner_is_guest({"name": "App", "upn": None, "type": "servicePrincipal", "guest": False})


# -- Storage -----------------------------------------------------------------------------------------


def _exchange_with_sizes(ann_bytes: int) -> dict:
    data = copy.deepcopy(gd.EXCHANGE)
    ann, info = data["Get-Mailbox"]
    ann.update({"ProhibitSendQuota": "49.5 GB (53,150,220,288 bytes)", "ArchiveStatus": "None"})
    info.update({"ProhibitSendQuota": "Unlimited"})
    data["Get-MailboxStatistics"] = {
        "g-u1": [
            {"TotalItemSize": f"x ({ann_bytes:,} bytes)", "TotalDeletedItemSize": "0 B (0 bytes)", "ItemCount": 9}
        ],
        "g-u7": [
            {"TotalItemSize": "586.6 KB (600,640 bytes)", "TotalDeletedItemSize": "0 B (0 bytes)", "ItemCount": 1}
        ],
    }
    return data


def test_a_mailbox_near_its_quota_is_found(db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha, exchange=_exchange_with_sizes(48 * GB))
    r = results(db, alpha)["mailbox_quota"]
    assert r.status == "fail" and r.summary == "1 mailbox at or over 90% of quota"
    assert [(f.subject, f.severity, f.object_id) for f in r.findings] == [("ann@x.com", "medium", "u1")]
    assert r.findings[0].detail == "48.0 GB of 49.5 GB (97%), no archive"
    seed_tenant(db, bravo, exchange=_exchange_with_sizes(10 * GB))
    r = results(db, bravo)["mailbox_quota"]
    assert r.status == "pass" and r.summary == "None of 1 mailbox is over 90% of its quota"  # Info has no quota


def test_a_full_mailbox_is_high_and_unread_sizes_are_unavailable(db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha, exchange=_exchange_with_sizes(50 * GB))
    r = results(db, alpha)["mailbox_quota"]
    assert r.findings[0].severity == "high" and r.findings[0].detail.startswith("50.0 GB of 49.5 GB (101%)")
    seed_tenant(db, bravo)  # the fixture has no mailbox statistics
    r = results(db, bravo)["mailbox_quota"]
    assert r.status == "unavailable" and r.summary == "Mailbox sizes couldn't be read"


def test_a_onedrive_near_its_quota_is_found(db, two_tenants, monkeypatch):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    r = results(db, alpha)["onedrive_quota"]
    assert r.status == "pass" and r.summary == "None of 3 OneDrives is over 90% of its quota"
    drives = copy.deepcopy(sharepoint_data.ONEDRIVE_USAGE)
    drives[1]["storageUsedInBytes"] = 950 * GB
    monkeypatch.setattr(sharepoint_data, "ONEDRIVE_USAGE", drives)
    seed_tenant(db, bravo)
    r = results(db, bravo)["onedrive_quota"]
    assert r.status == "fail" and r.summary == "1 OneDrive at or over 90% of quota"
    assert [(f.subject, f.severity) for f in r.findings] == [("bob@x.com", "medium")]
    assert r.findings[0].detail == "950.0 GB of 1.00 TB (93%)"


def test_storage_checks_without_the_licences(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, sharepoint_kw={"files": False})
    r = results(db, alpha)
    assert r["onedrive_quota"].status in ("pass", "unavailable")  # usage reports still answer without site access
    db.execute("DELETE FROM runs WHERE collector IN ('sharepoint_usage', 'mailbox_usage') AND tenant_id = ?", (alpha,))
    r = results(db, alpha)
    assert r["onedrive_quota"].summary == "SharePoint and OneDrive usage hasn't been collected yet"
    assert r["mailbox_quota"].summary == "Mailbox sizes haven't been collected yet"


# -- DKIM behind a gateway ---------------------------------------------------------------------------


def test_dkim_is_judged_for_a_gateway_domain_microsoft_365_sends_for():
    behind = dnscheck.check_domain(
        "g.com",
        resolver(
            {
                ("g.com", "MX"): ["eu-smtp-inbound-1.mimecast.com", "eu-smtp-inbound-2.mimecast.com"],
                ("g.com", "TXT"): ["v=spf1 include:eu._netblocks.mimecast.com include:spf.protection.outlook.com -all"],
                ("_dmarc.g.com", "TXT"): ["v=DMARC1; p=reject; rua=mailto:d@g.com"],
            }
        ),
    )
    assert behind.behind_gateway and behind.sends_from_microsoft_365 and not behind.microsoft_365
    dkim = [i for i in dnscheck.issues(behind) if i.area == "DKIM"]
    assert [i.severity for i in dkim] == ["medium"]
    assert dkim[0].detail.startswith(
        "Microsoft 365 DKIM isn't set up (selector1, selector2 CNAME missing). Mail arrives"
    )
    assert dnscheck.summary(behind)["DKIM"] == "Missing"

    only_gateway = dnscheck.check_domain(
        "h.com",
        resolver(
            {
                ("h.com", "MX"): ["mx1.hosted-gateway.example"],
                ("h.com", "TXT"): ["v=spf1 include:_spf.hosted-gateway.example -all"],
                ("_dmarc.h.com", "TXT"): ["v=DMARC1; p=reject; rua=mailto:d@h.com"],
            }
        ),
    )
    assert not only_gateway.sends_from_microsoft_365 and not only_gateway.behind_gateway
    assert not any(i.area == "DKIM" for i in dnscheck.issues(only_gateway))
    assert dnscheck.summary(only_gateway)["DKIM"] == "n/a"


def test_the_dkim_check_counts_gateway_domains(db, two_tenants):
    alpha, _ = two_tenants
    dns = dict(gd.DNS)
    dns[("x.com", "MX")] = ["eu-smtp-inbound-1.mimecast.com"]
    del dns[("selector1._domainkey.x.com", "CNAME")]
    seed_tenant(db, alpha, dns=lambda name, rdtype: list(dns.get((name, rdtype), [])))
    r = results(db, alpha)["dkim_enabled"]
    assert r.status == "fail" and r.summary == "1 mail domain without DKIM (1 behind a mail gateway)"
    assert r.findings[0].subject == "x.com" and "gateway" in r.findings[0].detail
    dns[("selector1._domainkey.x.com", "CNAME")] = ["s1"]
    dns[("selector2._domainkey.x.com", "CNAME")] = ["s2"]
    seed_tenant(db, alpha, dns=lambda name, rdtype: list(dns.get((name, rdtype), [])))
    r = results(db, alpha)["dkim_enabled"]
    assert (
        r.status == "pass"
        and r.summary == "DKIM is on for the 1 domain Microsoft 365 sends for (1 behind a mail gateway)"
    )
