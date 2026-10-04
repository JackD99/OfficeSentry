"""Fictional demo MSP data for the README screenshots and example reports.

``populate(db, vault)`` fills a freshly migrated database with an MSP
("Northgate IT") and four client tenants ("Alpha Dental Ltd" and others) on
``.example`` domains, every person made up. Nothing talks to Microsoft:
every collector runs against recorded-style Graph responses (as
``tests/seed.py`` does), built here per tenant. Each tenant is collected twice,
five weeks apart, so trends and "What changed" have history.

Used by ``scripts/screenshots.py``. Development only: it imports the test
fixtures, which aren't in the Docker image.
"""

from __future__ import annotations

import json
import random
import re
import zlib
from datetime import UTC, datetime, timedelta

import responses

from officesentry import prospect, store
from officesentry.certs import generate_certificate
from officesentry.db import utcnow
from officesentry.reports import branding
from tests import adminactivitydata as ad
from tests import devicedata as dd
from tests import graphdata as gd
from tests import groupdata as grp
from tests import hybriddata as hd
from tests import sharepoint_data as sp
from tests import tenantsettingsdata as tsd
from tests import test_collectors as tc
from tests import usagedata as ud

FIXTURES = (gd, dd, grp, hd, tsd, ad, sp, ud, tc)
GB = 1024 ** 3
MB = 1024 ** 2
TB = 1024 ** 4

MSP = {"brand_name": "Northgate IT", "brand_accent": "#0f6e5c",
       "brand_contact": "support@northgate-it.example · 0161 496 0000", "currency": "GBP"}
# Monthly prices (GBP), so licence waste and right-sizing show money.
PRICES = {"SPB": 18.10, "O365_BUSINESS_PREMIUM": 10.30, "O365_BUSINESS_ESSENTIALS": 4.90, "EXCHANGESTANDARD": 3.30}
PRODUCTS = {"sku-bp": "MICROSOFT 365 BUSINESS PREMIUM", "sku-std": "MICROSOFT 365 BUSINESS STANDARD",
            "sku-basic": "MICROSOFT 365 BUSINESS BASIC", "sku-exo": "EXCHANGE ONLINE (PLAN 1)"}

FIRST = ["Olivia", "James", "Amelia", "Oliver", "Isla", "George", "Ava", "Harry", "Mia", "Noah", "Sophie", "Jack",
         "Grace", "Leo", "Freya", "Arthur", "Emily", "Oscar", "Lily", "Charlie", "Ella", "Henry", "Ruby", "Thomas",
         "Chloe", "William", "Evie", "Alfie", "Poppy", "Jacob", "Zara", "Samuel", "Imogen", "Daniel", "Maya",
         "Joseph", "Priya", "Aaron", "Hannah", "Rhys", "Megan", "Owen", "Niamh", "Callum", "Aisha", "Ethan"]
LAST = ["Ashby", "Brennan", "Calloway", "Dunmore", "Ellery", "Fairbairn", "Garside", "Holloway", "Ingram", "Jarvis",
        "Kendrick", "Lockwood", "Marsden", "Norcott", "Oakley", "Pendry", "Quayle", "Rowntree", "Sefton", "Thornbury",
        "Upton", "Varley", "Whitlock", "Yardley", "Abbott", "Birch", "Crane", "Delaney", "Everett", "Fenwick",
        "Goodall", "Hartley", "Iqbal", "Kaur", "Lindqvist", "Mensah", "Nakamura", "Okafor", "Petrov", "Rahman"]
DEPARTMENTS = ["Reception", "Finance", "Operations", "Clinical", "Admin", "Marketing", "Projects", "Management"]

# Fixture people, by their local part in tests/*.py, in the order their new names are handed out.
FIXTURE_PEOPLE = ("ann", "bob", "carl", "carol", "dan", "erin", "eve", "mallory")
FIXTURE_IDS = {"u1": "ann", "u2": "bob", "u5": "eve", "u6": "carl"}
GROUP_MAILS = {"g1": "sales", "g2": "project-phoenix", "g3": "marketing", "g4": "board", "g5": "provisioned",
               "m1": "finance-alerts", "d1": "info"}

# name, domain label, staff, MFA share (5 weeks ago, now), inactive staff, Secure Score (then, now),
# SharePoint used (TB, then and now), extras.
TENANTS = [
    dict(name="Alpha Dental Ltd", base="alphadental", staff=31, mfa=(0.58, 0.74), inactive=3, score=(41.0, 52.5),
         storage=(1.11, 1.19), sites="dental", ca="mfa", forwards=2, bp_spare=3),
    dict(name="Brightwater Accountants", base="brightwater", staff=44, mfa=(0.39, 0.45), inactive=6,
         score=(28.0, 31.5), storage=(0.42, 0.47), sites="office", ca="none", forwards=3, bp_spare=6),
    dict(name="Cedar & Finch Solicitors LLP", base="cedarfinch", staff=22, mfa=(0.91, 0.97), inactive=1,
         score=(63.0, 71.0), storage=(0.35, 0.38), sites="office", ca="strict", forwards=0, bp_spare=1,
         hybrid=True),
    dict(name="Harbour View Hotels", base="harbourview", staff=57, mfa=(0.62, 0.69), inactive=5, score=(44.0, 47.0),
         storage=(0.61, 0.66), sites="office", ca="defaults", forwards=1, bp_spare=4),
]

# Alpha Dental's SharePoint: what fills a tenant that is nearly out of space.
DENTAL_SITES = [
    # slug, title, share of storage, recycle bin GB, template
    ("imaging", "Imaging Archive", 0.37, 46, "Team Site"),
    ("patient-records", "Patient Records", 0.27, 12, "Group"),
    ("practice", "Practice Management", 0.14, 6, "Group"),
    ("finance", "Finance", 0.09, 3, "Group"),
    ("marketing", "Marketing", 0.05, 1, "Group"),
    ("old-projects", "Old Projects", 0.05, 0, "Site Page Publishing"),
    ("intranet", "Staff Intranet", 0.03, 0, "Site Page Publishing"),
]
OFFICE_SITES = [
    ("clients", "Clients", 0.44, 4, "Group"),
    ("finance", "Finance", 0.22, 2, "Group"),
    ("operations", "Operations", 0.16, 1, "Group"),
    ("marketing", "Marketing", 0.10, 0, "Group"),
    ("intranet", "Staff Intranet", 0.08, 0, "Site Page Publishing"),
]


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def local_part(full_name: str) -> str:
    first, _, last = full_name.partition(" ")
    return re.sub(r"[^a-z.]", "", f"{first}.{last}".lower())


class Tenant:
    """One fictional client: its people and every Graph and Exchange response the collectors ask for."""

    def __init__(self, spec: dict, now: datetime):
        self.spec, self.now = spec, now
        self.domain = f"{spec['base']}.example"
        rng = random.Random(spec["name"])  # noqa: S311 (demo data, not security)
        names: list[str] = []
        while len(names) < spec["staff"] + len(FIXTURE_PEOPLE):
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            # The fixture people's first names are matched in text, so those must be unique.
            first_names = {n.split()[0] for n in names[:len(FIXTURE_PEOPLE)]}
            if name not in names and name.split()[0] not in first_names:
                names.append(name)
        self.people = dict(zip(FIXTURE_PEOPLE, names, strict=False))
        self.staff_names = names[len(FIXTURE_PEOPLE):]

    # -- Renaming the shared test fixtures ----------------------------------------------

    def renamer(self):
        base, domain = self.spec["base"], self.domain
        subs = [(re.compile(rf"\b{old}(?=@x\.)"), local_part(new)) for old, new in self.people.items()]
        subs += [(re.compile(rf"\b{uid}(?=@x\.)"), local_part(self.people[who])) for uid, who in FIXTURE_IDS.items()]
        subs += [(re.compile(rf"\b{gid}(?=@x\.)"), slug) for gid, slug in GROUP_MAILS.items()]
        subs += [(re.compile(r"\bbg(?=@x\.)"), "breakglass"), (re.compile(r"\bboss(?=@x\.)"), "practice.manager"),
                 (re.compile(r"x-com\b"), f"{base}-example"),
                 (re.compile(r"(?<![\w-])x\.com\b"), domain),
                 (re.compile(r"(?<![\w-])x\.co\.uk\b"), f"{base}-group.example"),
                 (re.compile(r"(?<![\w-])x(?=(\.onmicrosoft\.com|\.sharepoint\.com|-my\.sharepoint|"
                             r"-admin\.sharepoint))"), base),
                 (re.compile(r"_x_com\b"), f"_{base}_example"),
                 (re.compile(r"\bparked\.com\b"), f"{base}-old.example"),
                 (re.compile(r"\bgmail\.com\b"), "homemail.example"),
                 (re.compile(r"\bsupplier\.com\b"), "dentalsupplies.example"),
                 (re.compile(r"\bother\.com\b"), "partner-firm.example"),
                 (re.compile(r"\bpartner\.org\b"), "consultants.example")]
        subs += [(re.compile(rf"\b{old.capitalize()}\b"), new) for old, new in self.people.items()]
        subs += [(re.compile(rf"\b{old.upper()}\b"), new.split()[1].upper()) for old, new in self.people.items()]
        subs += [(re.compile(r"^Breakglass$"), "Emergency access"), (re.compile(r"^Guest$"), "Sam Carter (guest)"),
                 (re.compile(r"^Org$"), self.spec["name"]), (re.compile(r"^Info$"), "Info")]

        def rename(value):
            if isinstance(value, str):
                for pattern, repl in subs:
                    value = pattern.sub(repl, value)
                return value
            if isinstance(value, dict):
                return {rename(k): rename(v) for k, v in value.items()}
            if isinstance(value, list):
                return [rename(v) for v in value]
            if isinstance(value, tuple):
                return tuple(rename(v) for v in value)
            return value
        return rename

    def upn(self, who: str) -> str:
        return f"{local_part(self.people[who])}@{self.domain}"

    # -- Directory --------------------------------------------------------------------

    def staff(self, first: bool) -> list[dict]:
        """Generated staff: two join after the first collection, one leaves."""
        rng = random.Random(self.spec["name"] + "staff")  # noqa: S311
        rows = []
        n = self.spec["staff"]
        for i, name in enumerate(self.staff_names[:n]):
            if first and i >= n - 2:
                continue  # joined since
            inactive = i < self.spec["inactive"]
            last = self.now - timedelta(days=rng.randint(95, 280) if inactive else rng.choice([0, 0, 0, 1, 1, 2, 3, 6]),
                                        hours=rng.randint(0, 9))
            sku = ("sku-std" if i % 7 == 3 else "sku-exo" if i % 9 == 5 else "sku-basic" if i % 6 == 4 else "sku-bp")
            upn = f"{local_part(name)}@{self.domain}"
            rows.append({"id": f"p{i:03d}", "displayName": name, "userPrincipalName": upn, "mail": upn,
                         "userType": "Member", "accountEnabled": not (i == 5 and not first),
                         "assignedLicenses": [{"skuId": sku}], "department": DEPARTMENTS[i % len(DEPARTMENTS)],
                         "jobTitle": None, "createdDateTime": iso(self.now - timedelta(days=60 + 41 * i)),
                         "onPremisesSyncEnabled": True if self.spec.get("hybrid") else None,
                         "signInActivity": {"lastSignInDateTime": iso(last)}, "_inactive": inactive, "_sku": sku})
        return rows

    def users(self, first: bool) -> list[dict]:
        synced = True if self.spec.get("hybrid") else None
        people = [
            ("u1", "ann", True, "sku-bp", 0), ("u2", "bob", False, "sku-bp", None), ("u5", "eve", True, "sku-bp", 1),
            ("u6", "carl", True, "sku-std", 2)]
        rows = [{"id": uid, "displayName": self.people[who], "userPrincipalName": self.upn(who),
                 "mail": self.upn(who), "userType": "Member", "accountEnabled": enabled,
                 "assignedLicenses": [{"skuId": sku}], "onPremisesSyncEnabled": synced,
                 "department": "Management", "createdDateTime": iso(self.now - timedelta(days=1400)),
                 "signInActivity": {"lastSignInDateTime": iso(self.now - timedelta(days=days, hours=3))}
                 if days is not None else {}} for uid, who, enabled, sku, days in people]
        rows += [
            {"id": "u3", "displayName": "Sam Carter (guest)", "userPrincipalName":
             f"sam.carter_consultants.example#EXT#@{self.spec['base']}.onmicrosoft.com", "mail":
             "sam.carter@consultants.example", "userType": "Guest", "accountEnabled": True, "assignedLicenses": [],
             "createdDateTime": iso(self.now - timedelta(days=500)),
             "signInActivity": {"lastSignInDateTime": iso(self.now - timedelta(days=160))}},
            {"id": "u4", "displayName": "Emergency access", "userPrincipalName": f"breakglass@{self.domain}",
             "userType": "Member", "accountEnabled": True, "assignedLicenses": [],
             "createdDateTime": iso(self.now - timedelta(days=900)),
             "signInActivity": {"lastSignInDateTime": iso(self.now - timedelta(days=41))}},
            {"id": "u7", "displayName": "Info", "userPrincipalName": f"info@{self.domain}", "mail":
             f"info@{self.domain}", "userType": "Member", "accountEnabled": True, "assignedLicenses": [],
             "createdDateTime": iso(self.now - timedelta(days=1500)), "signInActivity": {}},
        ]
        rows += [{k: v for k, v in u.items() if not k.startswith("_")} for u in self.staff(first)]
        return rows

    def registration(self, first: bool) -> list[dict]:
        share = self.spec["mfa"][0 if first else 1]
        rng = random.Random(self.spec["name"] + "mfa")  # noqa: S311
        rows = [
            {"id": "u1", "userPrincipalName": self.upn("ann"), "userDisplayName": self.people["ann"],
             "userType": "member", "isAdmin": True, "isMfaRegistered": True, "isMfaCapable": True,
             "methodsRegistered": ["microsoftAuthenticatorPush", "mobilePhone"]},
            {"id": "u4", "userPrincipalName": f"breakglass@{self.domain}", "userDisplayName": "Emergency access",
             "userType": "member", "isAdmin": True, "isMfaRegistered": False, "isMfaCapable": False,
             "methodsRegistered": []},
            {"id": "u5", "userPrincipalName": self.upn("eve"), "userDisplayName": self.people["eve"],
             "userType": "member", "isAdmin": True, "isMfaRegistered": not first, "isMfaCapable": not first,
             "methodsRegistered": ["microsoftAuthenticatorPush"] if not first else []},
            {"id": "u6", "userPrincipalName": self.upn("carl"), "userDisplayName": self.people["carl"],
             "userType": "member", "isAdmin": False, "isMfaRegistered": False, "isMfaCapable": False,
             "methodsRegistered": []},
        ]
        for u in self.staff(first):
            registered = rng.random() < share and not u["_inactive"]
            methods = rng.choice([["microsoftAuthenticatorPush"], ["microsoftAuthenticatorPush", "mobilePhone"],
                                  ["mobilePhone"], ["windowsHelloForBusiness", "microsoftAuthenticatorPush"]])
            rows.append({"id": u["id"], "userPrincipalName": u["userPrincipalName"],
                         "userDisplayName": u["displayName"], "userType": "member", "isAdmin": False,
                         "isMfaRegistered": registered, "isMfaCapable": registered,
                         "methodsRegistered": methods if registered else []})
        return rows

    def skus(self, users: list[dict]) -> list[dict]:
        def assigned(sku):
            return sum(1 for u in users if {"skuId": sku} in u.get("assignedLicenses", []))
        plans = {"sku-bp": ["AAD_PREMIUM", "INTUNE_A", "EXCHANGE_S_STANDARD", "SHAREPOINTSTANDARD"],
                 "sku-std": ["EXCHANGE_S_STANDARD", "SHAREPOINTSTANDARD", "OFFICESUBSCRIPTION"],
                 "sku-basic": ["EXCHANGE_S_STANDARD", "SHAREPOINTSTANDARD"],
                 "sku-exo": ["EXCHANGE_S_STANDARD"]}
        parts = {"sku-bp": "SPB", "sku-std": "O365_BUSINESS_PREMIUM", "sku-basic": "O365_BUSINESS_ESSENTIALS",
                 "sku-exo": "EXCHANGESTANDARD"}
        spare = {"sku-bp": self.spec["bp_spare"], "sku-std": 1, "sku-basic": 2, "sku-exo": 0}
        rows = [{"skuId": sku, "skuPartNumber": parts[sku], "appliesTo": "User", "capabilityStatus": "Enabled",
                 "prepaidUnits": {"enabled": assigned(sku) + spare[sku]}, "consumedUnits": assigned(sku),
                 "servicePlans": [{"servicePlanName": p, "provisioningStatus": "Success"} for p in plans[sku]]}
                for sku in parts if assigned(sku)]
        return rows + [tc.SKUS[1]]

    def conditional_access(self) -> tuple[list[dict], bool]:
        kind = self.spec["ca"]
        if kind == "none":
            return [], False
        if kind == "defaults":
            return [], True
        if kind == "strict":
            return [gd.CA_MFA_ALL, gd.CA_BLOCK_LEGACY], False
        return [gd.CA_MFA_ALL, gd.CA_REPORT_ONLY], False

    def secure_scores(self, first: bool) -> list[dict]:
        start, end = self.spec["score"]
        days = 70 if first else 105
        target = start if first else end
        rows = []
        for d in range(days, -1, -1):
            progress = (days - d) / days
            score = round(start - 5 + (target - start + 5) * progress ** 1.5, 1)
            rows.append({"createdDateTime": (self.now - timedelta(days=d)).strftime("%Y-%m-%dT00:00:00Z"),
                         "currentScore": score, "maxScore": 100.0,
                         "controlScores": gd.SECURE_SCORES_HISTORY[0]["controlScores"]})
        return list(reversed(rows))

    # -- Exchange -------------------------------------------------------------------------

    def exchange(self, users: list[dict], first: bool) -> dict:
        rng = random.Random(self.spec["name"] + "exo")  # noqa: S311
        quota = "49.5 GB (53,150,220,288 bytes)"
        boxes, stats, cas, perms, send_as = [], {}, [], {}, []
        people = [u for u in users if u["userType"] == "Member" and u.get("assignedLicenses")]
        forwards = 0 if first else self.spec["forwards"]
        for i, u in enumerate(people):
            guid = f"g-{u['id']}"
            box = {"ExternalDirectoryObjectId": u["id"], "Guid": guid, "UserPrincipalName": u["userPrincipalName"],
                   "PrimarySmtpAddress": u["userPrincipalName"], "DisplayName": u["displayName"],
                   "Name": u["id"], "Alias": u["userPrincipalName"].split("@")[0],
                   "RecipientTypeDetails": "UserMailbox", "GrantSendOnBehalfTo": [], "ProhibitSendQuota": quota,
                   "ArchiveStatus": "Active" if i % 4 == 0 else "None"}
            if u["id"] == "u1":
                box.update(ForwardingSmtpAddress=f"smtp:{local_part(u['displayName'])}@homemail.example",
                           DeliverToMailboxAndForward=True)
            elif u["id"].startswith("p") and 0 < forwards and i % 11 == 6:
                forwards -= 1
                box.update(ForwardingSmtpAddress=f"smtp:{u['userPrincipalName'].split('@')[0]}@homemail.example",
                           DeliverToMailboxAndForward=False)
            boxes.append(box)
            size = 47.2 if i == 2 else 45.6 if i == 9 else rng.uniform(1.5, 32)
            stats[guid] = [{"TotalItemSize": f"{size:.1f} GB ({int(size * GB):,} bytes)",
                            "TotalDeletedItemSize": f"{size / 20:.1f} GB ({int(size * GB / 20):,} bytes)",
                            "ItemCount": int(size * 2400)}]
            perms[guid] = [{"User": "NT AUTHORITY\\SELF", "AccessRights": ["FullAccess"], "IsInherited": False}]
            cas.append({"ExternalDirectoryObjectId": u["id"], "Guid": guid, "PrimarySmtpAddress": u["userPrincipalName"],
                        "SmtpClientAuthenticationDisabled": None if i % 8 == 1 else True,
                        "PopEnabled": i % 10 == 3, "ImapEnabled": i % 5 == 2})
        staff_upns = [u["userPrincipalName"] for u in people if u["id"].startswith("p") and u["accountEnabled"]]
        shared = [("u7", "info", "Info", "SharedMailbox"), ("x-rec", "reception", "Reception", "SharedMailbox"),
                  ("x-acc", "accounts", "Accounts", "SharedMailbox"),
                  ("x-room", "boardroom", "Boardroom", "RoomMailbox")]
        for sid, alias, display, kind in shared:
            guid = f"g-{sid}"
            addr = f"{alias}@{self.domain}"
            boxes.append({"ExternalDirectoryObjectId": sid, "Guid": guid, "UserPrincipalName": addr,
                          "PrimarySmtpAddress": addr, "DisplayName": display, "Name": display, "Alias": alias,
                          "RecipientTypeDetails": kind, "ProhibitSendQuota": quota,
                          "GrantSendOnBehalfTo": [self.people["ann"]] if alias == "info" else []})
            size = {"info": 6.4, "reception": 18.9, "accounts": 44.8, "boardroom": 0.1}[alias]
            stats[guid] = [{"TotalItemSize": f"{size:.1f} GB ({int(size * GB):,} bytes)",
                            "TotalDeletedItemSize": "0 B (0 bytes)", "ItemCount": int(size * 3000)}]
            if kind == "RoomMailbox":
                continue
            who = [self.upn("ann")] + rng.sample(staff_upns, 3)
            if alias == "accounts":
                who += [self.upn("bob"), "sam.carter@consultants.example"]  # a leaver and an outsider keep access
            perms[guid] = [{"User": w, "AccessRights": ["FullAccess"], "IsInherited": False} for w in who]
            send_as += [{"Identity": display, "Trustee": w, "AccessRights": ["SendAs"]} for w in who[:2]]
            cas.append({"ExternalDirectoryObjectId": sid, "Guid": guid, "PrimarySmtpAddress": addr,
                        "SmtpClientAuthenticationDisabled": None if alias == "reception" else True,
                        "PopEnabled": alias == "info", "ImapEnabled": alias == "info"})
        return {
            "Get-AcceptedDomain": [{"DomainName": self.domain, "DomainType": "Authoritative", "Default": True}],
            "Get-Mailbox": boxes, "Get-MailboxPermission": perms, "Get-MailboxStatistics": stats,
            "Get-CASMailbox": cas, "Get-RecipientPermission": send_as,
            "Get-OrganizationConfig": [{"AuditDisabled": False}],
            "Get-TransportConfig": [{"SmtpClientAuthenticationDisabled": self.spec["ca"] == "strict"}],
            "Get-HostedOutboundSpamFilterPolicy": [{"Name": "Default", "IsDefault": True,
                                                    "AutoForwardingMode": "Off" if self.spec["ca"] == "strict"
                                                    else "Automatic"}],
            "Get-RemoteDomain": [{"Name": "Default", "DomainName": "*", "AutoForwardEnabled": True}],
            "Get-Recipient": [],
        }

    def inbox_rules(self, users: list[dict]) -> dict:
        rules = {"u1": gd.INBOX_RULES["u1"], "u7": gd.INBOX_RULES["u7"]}
        for i, u in enumerate(u for u in users if u["id"].startswith("p")):
            if i % 4 == 1:
                rules[u["id"]] = [{"id": f"r-{u['id']}", "displayName": "Newsletters", "isEnabled": True,
                                   "sequence": 1, "conditions": {"senderContains": ["newsletter"]},
                                   "actions": {"moveToFolder": "fid-news", "stopProcessingRules": False}}]
            if i == 7:
                rules[u["id"]] = [{"id": f"r-{u['id']}-fwd", "displayName": "Send invoices to bookkeeper",
                                   "isEnabled": True, "sequence": 1, "conditions": {"subjectContains": ["invoice"]},
                                   "actions": {"forwardTo": [{"emailAddress": {
                                       "address": "bookkeeping@ledgerline.example"}}]}}]
        return rules

    # -- Devices --------------------------------------------------------------------------

    def devices(self, users: list[dict]) -> tuple[list[dict], list[dict]]:
        rng = random.Random(self.spec["name"] + "dev")  # noqa: S311
        entra, managed = list(dd.ENTRA_DEVICES), list(dd.MANAGED_DEVICES)
        builds = ["10.0.26100.6584", "10.0.26100.4946", "10.0.22631.5909", "10.0.22631.5189", "10.0.19045.6332"]
        models = [("Dell", "Latitude 7450"), ("Lenovo", "ThinkPad T14 Gen 5"), ("HP", "EliteBook 840 G10"),
                  ("Microsoft", "Surface Laptop 6"), ("Dell", "OptiPlex 7010")]
        for i, u in enumerate(u for u in users if u["id"].startswith("p") and u["accountEnabled"]):
            name = f"{'DESK' if i % 3 == 0 else 'LAPTOP'}-{u['displayName'].split()[1].upper()[:8]}{i:02d}"
            build = builds[0 if i % 6 == 0 else 4 if i % 9 == 4 else rng.randrange(4)]
            sync = 0 if i % 13 else 38
            compliant = i % 11 != 7 and sync < 30
            make, model = models[i % len(models)]
            entra.append({"id": f"ed-{u['id']}", "deviceId": f"d-{u['id']}", "displayName": name,
                          "operatingSystem": "Windows", "operatingSystemVersion": build, "trustType": "AzureAd",
                          "isManaged": True, "isCompliant": compliant, "accountEnabled": True,
                          "approximateLastSignInDateTime": dd.days_ago(sync + rng.randint(0, 2)),
                          "registrationDateTime": dd.days_ago(200 + 30 * i)})
            managed.append({"id": f"md-{u['id']}", "deviceName": name, "operatingSystem": "Windows",
                            "osVersion": build, "complianceState": "compliant" if compliant else "noncompliant",
                            "isEncrypted": i % 11 != 7, "lastSyncDateTime": dd.days_ago(sync),
                            "enrolledDateTime": dd.days_ago(200 + 30 * i), "userPrincipalName":
                            u["userPrincipalName"], "managedDeviceOwnerType": "company", "manufacturer": make,
                            "model": model, "jailBroken": "Unknown", "azureADDeviceId": f"d-{u['id']}"})
        return entra, managed

    # -- Microsoft 365 usage ------------------------------------------------------------

    def usage(self, users: list[dict]) -> dict:
        rng = random.Random(self.spec["name"] + "use")  # noqa: S311
        active, apps, email, teams, installs = [], [], [], [], []
        for i, u in enumerate(u for u in users if u["userType"] == "Member" and u.get("assignedLicenses")):
            sku = u["assignedLicenses"][0]["skuId"]
            upn, name = u["userPrincipalName"], u["displayName"]
            inactive = not u["accountEnabled"] or "lastSignInDateTime" not in u.get("signInActivity", {}) or \
                datetime.fromisoformat(u["signInActivity"]["lastSignInDateTime"].replace("Z", "+00:00")) < \
                self.now - timedelta(days=60)
            email_only = sku == "sku-bp" and i % 8 == 5
            web_only = sku == "sku-std" or (sku == "sku-bp" and i % 10 == 7)
            if inactive:
                active.append(ud.active_user(upn, [PRODUCTS[sku]], name=name))
                apps.append(ud.app_user(upn))
                continue
            seen = rng.choice([0, 1, 1, 2, 3])
            if email_only or sku in ("sku-exo",):
                active.append(ud.active_user(upn, [PRODUCTS[sku]], name=name, exchange=seen))
                apps.append(ud.app_user(upn, seen, outlook=("Web", "Mobile")))
            else:
                active.append(ud.active_user(upn, [PRODUCTS[sku]], name=name, exchange=seen, teams=seen,
                                             onedrive=rng.choice([1, 2, 5, 9]),
                                             sharepoint=rng.choice([1, 3, 6, None])))
                if web_only or sku == "sku-basic":
                    apps.append(ud.app_user(upn, seen, outlook=("Web",), word=("Web",), teams=("Web", "Mobile")))
                else:
                    plat = ("Mac",) if i % 9 == 2 else ("Windows",)
                    apps.append(ud.app_user(upn, seen, outlook=plat + ("Mobile",), word=plat, excel=plat,
                                            powerPoint=plat if i % 3 else (), teams=plat + ("Mobile",)))
                    installs.append({"reportRefreshDate": ud.REFRESHED, "userPrincipalName": upn, "displayName": name,
                                     "userActivationCounts": [{
                                         "productType": "MICROSOFT 365 APPS FOR BUSINESS",
                                         "lastActivatedDate": ud.days_ago(rng.randint(20, 400)),
                                         "windows": 0 if "Mac" in plat else rng.choice([1, 1, 2]),
                                         "mac": 1 if "Mac" in plat else 0, "windows10Mobile": 0,
                                         "ios": rng.choice([0, 1]), "android": rng.choice([0, 0, 1]),
                                         "activatedOnSharedComputer": False}]})
                teams.append({"reportRefreshDate": ud.REFRESHED, "userId": u["id"], "userPrincipalName": upn,
                              "lastActivityDate": ud.days_ago(seen), "isDeleted": False, "isExternal": False,
                              "assignedProducts": [], "teamChatMessageCount": rng.randint(5, 220),
                              "privateChatMessageCount": rng.randint(20, 600), "callCount": rng.randint(0, 40),
                              "meetingCount": rng.randint(1, 35), "isLicensed": True, "reportPeriod": "30"})
            received = rng.randint(250, 1900)
            email.append({"reportRefreshDate": ud.REFRESHED, "userPrincipalName": upn, "displayName": name,
                          "isDeleted": False, "lastActivityDate": ud.days_ago(seen),
                          "sendCount": rng.randint(20, 400), "receiveCount": received,
                          "readCount": int(received * rng.uniform(0.6, 0.95)), "meetingCreatedCount": 0,
                          "meetingInteractedCount": 0, "assignedProducts": [], "reportPeriod": "30"})
        return {"ACTIVE_USERS": active, "APP_USERS": apps, "EMAIL": email, "TEAMS": teams, "ACTIVATIONS": installs}

    # -- SharePoint and OneDrive --------------------------------------------------------------

    def sharepoint(self, users: list[dict], first: bool) -> dict:
        rng = random.Random(self.spec["name"] + "spo")  # noqa: S311
        base = self.spec["base"]
        host = f"https://{base}.sharepoint.com"
        used_tb = self.spec["storage"][0 if first else 1]
        total = int(used_tb * TB)
        layout = DENTAL_SITES if self.spec["sites"] == "dental" else OFFICE_SITES
        sites, drives, quotas, lists, items, recycle, usage = [], {}, {}, {}, {}, {}, []
        delta, permissions = {}, {}
        owner = self.people["ann"]
        for slug, title, share, bin_gb, template in layout:
            sid, url = ("site-fin" if slug == "finance" else f"site-{slug}"), f"{host}/sites/{slug}"
            size = int(total * share)
            sites.append({"id": sid, "displayName": title, "webUrl": url})
            drives[sid] = [{"id": f"drv-{slug}", "name": "Documents", "webUrl": f"{url}/Shared Documents",
                            "driveType": "documentLibrary"}]
            delta[f"drv-{slug}"] = []
            quotas[sid] = {"used": size, "deleted": bin_gb * GB, "total": 27487790694400, "state": "normal"}
            lists[sid] = [{"id": f"lst-{slug}", "name": "Shared Documents", "displayName": "Documents",
                           "webUrl": f"{url}/Shared%20Documents",
                           "list": {"template": "documentLibrary", "hidden": False}}]
            hold = int(size * 0.12) if slug in ("patient-records", "finance", "clients") else 0
            if hold:
                lists[sid].append({"id": f"lst-{slug}-hold", "name": "PreservationHoldLibrary",
                                   "displayName": "Preservation Hold Library",
                                   "webUrl": f"{url}/PreservationHoldLibrary",
                                   "list": {"template": "documentLibrary", "hidden": False}, "system": {}})
                items[f"lst-{slug}-hold"] = [self._item(f"/sites/{slug}/PreservationHoldLibrary/{name}", total=s,
                                                        modified=400) for name, s in (
                    ("Contracts archive_v12.zip", hold // 2), ("Board pack 2023_v8.pdf", hold // 3))]
            items[f"lst-{slug}"] = self._library(slug, size - hold, rng)
            recycle[sid] = [] if not bin_gb else [
                {"id": f"rb-{slug}-{n}", "name": name, "size": int(bin_gb * GB * part),
                 "deletedDateTime": iso(self.now - timedelta(days=days)),
                 "deletedFromLocation": f"sites/{slug}/Shared Documents/{folder}"}
                for n, (name, part, days, folder) in enumerate((
                    ("Scanner exports 2022.zip", 0.55, 12, "Scans"), ("Old intraoral camera images", 0.3, 33, "Imaging"),
                    ("Copy of Copy of Rota.xlsx", 0.15, 4, "Rotas")))]
            usage.append(sp._site(url, template, activity=rng.choice([0, 1, 2, 4]) if slug != "old-projects" else 410,
                                  storage=size, anyone=2 if slug == "marketing" else 0,
                                  guest=1 if slug in ("finance", "clients") else 0, company=3, owner=owner))
        # Shared files: the fixture's Finance library and Ann's OneDrive, renamed.
        delta["drv-fin"] = sp.DELTA["drv-fin"]
        drives["site-fin"] = sp.DRIVES["site-fin"]
        sites.append(sp.SITES[1])
        drives[sp.SITES[1]["id"]] = sp.DRIVES[sp.SITES[1]["id"]]
        delta["drv-ann"] = sp.DELTA["drv-ann"]
        growth = (self.spec["storage"][1] - self.spec["storage"][0]) * TB / 35
        history = [{"reportRefreshDate": sp.days_ago(2), "siteType": "All", "reportDate": sp.days_ago(2 + d),
                    "storageUsedInBytes": int(total - d * growth), "reportPeriod": "180"} for d in range(0, 180, 5)]
        drive_usage = [sp._drive(u["userPrincipalName"], int(rng.uniform(0.5, 60) * GB), activity=rng.choice([1, 2, 4]))
                       for u in users if u["userType"] == "Member" and u.get("assignedLicenses")]
        return {"SITES": sites, "DRIVES": drives, "SITE_QUOTA": quotas, "LISTS": lists, "LIST_ITEMS": {
            **items, "list-ann": sp.LIST_ITEMS["list-ann"]}, "RECYCLE_BIN": recycle, "SITE_USAGE": usage,
            "SITE_STORAGE": history, "ONEDRIVE_USAGE": drive_usage, "DELTA": delta,
            "PERMISSIONS": {**sp.PERMISSIONS, **permissions}}

    def _item(self, ref: str, *, total: int, current: int | None = None, version: str = "1.0", folder=False,
              files: int = 0, modified: int = 10, by: str | None = None):
        fields = {"FileLeafRef": ref.rsplit("/", 1)[-1], "FileRef": ref, "FSObjType": "1" if folder else "0",
                  "SMTotalSize": str(total),
                  "SMTotalFileStreamSize": str(0 if folder else (total if current is None else current)),
                  "SMTotalFileCount": str(files), "_UIVersionString": version}
        who = by or self.upn("ann")
        return {"id": str(zlib.crc32(ref.encode())),
                "webUrl": f"https://{self.spec['base']}.sharepoint.com" + ref.replace(" ", "%20"),
                "lastModifiedDateTime": iso(self.now - timedelta(days=modified, hours=4)),
                "lastModifiedBy": {"user": {"email": who, "displayName": next(
                    (n for k, n in self.people.items() if self.upn(k) == who), who)}}, "fields": fields}

    def _library(self, slug: str, size: int, rng: random.Random) -> list[dict]:
        """Folders and files whose sizes add up to ``size``: big old files, files with long version history."""
        root = f"/sites/{slug}/Shared Documents"
        dental = self.spec["sites"] == "dental"
        folders = {
            "imaging": ["X-rays 2019", "X-rays 2020", "CBCT scans", "Intraoral photos", "Referral images"],
            "patient-records": ["Consent forms", "Treatment plans", "Lab dockets", "Correspondence"],
            "practice": ["Rotas", "Policies", "Compliance", "Equipment manuals"],
        }.get(slug, ["Projects", "Archive", "Templates", "Shared", "Scans"])
        people = [self.upn(k) for k in ("ann", "carl", "eve", "carol")]
        out, left = [], size
        for n, folder in enumerate(folders):
            part = left * (0.42 if n < len(folders) - 1 else 1.0)
            left -= part
            path = f"{root}/{folder}"
            count = rng.randint(400, 9000)
            out.append(self._item(path, total=int(part), folder=True, files=count, modified=rng.randint(1, 60)))
            big = [("archive.zip", 0.18, "1.0", 900), ("export.pst", 0.07, "1.0", 1100)] if n % 2 == 0 else \
                [("master.xlsx", 0.04, "640.0", 2), ("planner.docx", 0.02, "212.0", 5)]
            for suffix, share, version, age in big:
                name = f"{folder} {suffix}" if dental else f"{folder} {suffix}"
                total = int(part * share)
                current = total if version == "1.0" else max(int(total / float(version) * 3), 8 * MB)
                out.append(self._item(f"{path}/{name}", total=total, current=current, version=version,
                                      modified=age, by=rng.choice(people)))
        if slug == "practice":
            out.append(self._item(f"{root}/Appointment book.xlsx", total=int(9.6 * GB), current=int(14 * MB),
                                  version="1184.0", modified=0, by=self.upn("carl")))
            out.append(self._item(f"{root}/Practice database backup.bak", total=int(31 * GB), modified=820))
        return out

    # -- DNS ----------------------------------------------------------------------------

    def dns(self) -> dict:
        d, b = self.domain, self.spec["base"]
        policy = {"strict": "reject", "mfa": "quarantine", "defaults": "none"}.get(self.spec["ca"])
        records = {
            (d, "MX"): [f"{b}-example.mail.protection.outlook.com"],
            (d, "TXT"): ["v=spf1 include:spf.protection.outlook.com -all" if policy else
                         "v=spf1 include:spf.protection.outlook.com include:mailhost.example ~all", "MS=ms4417291"],
            ("spf.protection.outlook.com", "TXT"): ["v=spf1 ip4:40.92.0.0/15 -all"],
        }
        if policy:
            records[(f"_dmarc.{d}", "TXT")] = [f"v=DMARC1; p={policy}; rua=mailto:dmarc@{d}"]
        if self.spec["ca"] != "none":
            records[(f"selector1._domainkey.{d}", "CNAME")] = [
                f"selector1-{b}-example._domainkey.{b}.onmicrosoft.com"]
            records[(f"selector2._domainkey.{d}", "CNAME")] = [
                f"selector2-{b}-example._domainkey.{b}.onmicrosoft.com"]
        return records

    # -- Everything, as the module constants the test mocks read -------------------------

    def overrides(self, first: bool) -> dict:
        users = self.users(first)
        entra, managed = self.devices(users)
        hybrid = self.spec.get("hybrid")
        org = {"id": f"org-{self.spec['base']}", "displayName": self.spec["name"],
               "verifiedDomains": [{"name": self.domain, "isDefault": True},
                                   {"name": f"{self.spec['base']}.onmicrosoft.com"}]}
        if hybrid:
            org.update(onPremisesSyncEnabled=True, onPremisesLastSyncDateTime=hd.hours_ago(0.4),
                       onPremisesLastPasswordSyncDateTime=hd.hours_ago(0.4))
        return {
            gd: {"ORGANIZATION": org, "EXCHANGE": self.exchange(users, first),
                 "FOLDERS": {**gd.FOLDERS, "fid-news": {"displayName": "Newsletters"}}},
            dd: {"ENTRA_DEVICES": entra, "MANAGED_DEVICES": managed},
            hd: {"DOMAINS": hd.HYBRID_DOMAINS if hybrid else hd.DOMAINS},
            sp: self.sharepoint(users, first),
            ud: self.usage(users),
            "kwargs": {"users": users, "skus": self.skus(users), "registration": self.registration(first),
                       "secure_scores": self.secure_scores(first), "inbox_rules": self.inbox_rules(users),
                       "ca": self.conditional_access()[0], "defaults": self.conditional_access()[1],
                       "per_user": {"u1": "enforced"},
                       "auth_policy": tsd.LOCKED_POLICY if self.spec["ca"] == "strict" else tsd.OPEN_POLICY},
        }


class Swapped:
    """Swap module constants for the duration of a collection, and put them back afterwards."""

    def __init__(self, values: dict):
        self.values, self.saved = values, []

    def __enter__(self):
        for module, names in self.values.items():
            for name, value in names.items():
                self.saved.append((module, name, getattr(module, name)))
                setattr(module, name, value)
        return self

    def __exit__(self, *exc):
        for module, name, value in reversed(self.saved):
            setattr(module, name, value)


def renamed_fixtures(rename) -> dict:
    return {m: {k: rename(v) for k, v in vars(m).items() if k.isupper() and isinstance(v, (dict, list, tuple, str))}
            for m in FIXTURES}


def collect(db, tenant_id: int, t: Tenant, first: bool) -> None:
    from tests.seed import seed_tenant

    with Swapped(renamed_fixtures(t.renamer())):
        over = t.overrides(first)
        kwargs = over.pop("kwargs")
        with Swapped(over):
            dns = t.dns()
            seed_tenant(db, tenant_id, dns=lambda name, rdtype: list(dns.get((name, rdtype), [])), **kwargs)


def backdate(db, tenant_id: int, days: int) -> None:
    for table, cols in (("runs", ("created_at", "started_at", "finished_at")), ("metrics", ("captured_at",)),
                        ("findings", ("first_seen", "last_seen"))):
        sets = ", ".join(f"{c} = strftime('%Y-%m-%dT%H:%M:%SZ', {c}, '-{days} days')" for c in cols)
        db.execute(f"UPDATE {table} SET {sets} WHERE tenant_id = ?", (tenant_id,))  # noqa: S608
    db.execute(f"UPDATE run_events SET ts = strftime('%Y-%m-%dT%H:%M:%SZ', ts, '-{days} days')"  # noqa: S608
               " WHERE run_id IN (SELECT id FROM runs WHERE tenant_id = ?)", (tenant_id,))


def logo_png(name: str, accent: str) -> bytes | None:
    """A simple wordmark, if Pillow is installed."""
    try:
        from io import BytesIO
        from pathlib import Path

        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    fonts = Path(__file__).resolve().parents[1] / "officesentry" / "web" / "static" / "fonts"
    img = Image.new("RGBA", (760, 180), (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)
    rgb = tuple(int(accent[i:i + 2], 16) for i in (1, 3, 5))
    draw.rounded_rectangle((10, 30, 130, 150), radius=28, fill=(*rgb, 255))
    draw.polygon([(36, 122), (70, 58), (104, 122)], fill=(255, 255, 255, 255))
    draw.polygon([(58, 122), (70, 98), (82, 122)], fill=(*rgb, 255))
    draw.text((160, 40), name, font=ImageFont.truetype(str(fonts / "Inter-Bold.ttf"), 82), fill=(16, 42, 67, 255))
    out = BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def add_prospect(db) -> None:
    """A prospect assessment for a fictional company, with Microsoft's sign-in discovery mocked."""
    domain = "kingsley-optometry.example"
    records = {(domain, "MX"): ["kingsley-optometry-example.mail.protection.outlook.com"],
               (domain, "TXT"): ["v=spf1 include:spf.protection.outlook.com include:bulkmail.example ?all"],
               ("spf.protection.outlook.com", "TXT"): ["v=spf1 ip4:40.92.0.0/15 -all"],
               (f"_dmarc.{domain}", "TXT"): ["v=DMARC1; p=none"]}
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.get(f"{prospect.LOGIN}/{domain}/v2.0/.well-known/openid-configuration",
                 json={"issuer": "https://login.microsoftonline.com/7c1e5f0a-3b9d-4e62-9a51-2f6c0d8e4b17/v2.0"})
        rsps.get(f"{prospect.LOGIN}/getuserrealm.srf", json={"NameSpaceType": "Managed",
                                                             "FederationBrandName": "Kingsley Optometry"})
        a = prospect.run(domain, "Kingsley Optometry", resolve=lambda n, t: list(records.get((n, t), [])))
    prospect.save(db, a, "demo")


def add_email(db, tenant_id: int, t: Tenant) -> None:
    """Contacts, a monthly report schedule with a delivery history, and an alert email."""
    from officesentry import delivery

    client = delivery.add_contact(db, tenant_id, t.people["ann"], t.upn("ann"))
    partner = delivery.add_contact(db, tenant_id, t.people["eve"], t.upn("eve"))
    team = delivery.add_contact(db, None, "Northgate IT service desk", "servicedesk@northgate-it.example")
    sid = db.execute(
        "INSERT INTO email_schedules (kind, tenant_id, reports, include_xlsx, message, frequency, day_of_month, hour,"
        " timezone, last_period, last_status, last_sent_at, created_by, created_at)"
        " VALUES ('report', ?, ?, 1, ?, 'monthly', 1, 8, 'Europe/London', ?, 'sent', ?, 'demo', ?)",
        (tenant_id, json.dumps(["review", "evidence", "data:sharepoint-storage", "data:licence-rightsizing"]),
         "Your monthly review is attached. Call us if anything needs a hand.",
         (t.now - timedelta(days=4)).strftime("%Y-%m"), iso(t.now - timedelta(days=3)), utcnow())).lastrowid
    delivery.set_recipients(db, "schedule", sid, [client, partner, team])
    alert = db.execute("INSERT INTO email_alerts (tenant_id, min_severity, created_by, created_at)"
                       " VALUES (?, 'high', 'demo', ?)", (tenant_id, utcnow())).lastrowid
    delivery.set_recipients(db, "alert", alert, [team])


def populate(db, vault) -> list[int]:
    """Add the demo tenants, their history, prices and MSP branding to a migrated database; returns tenant ids."""
    now = datetime.now(UTC)
    cid = store.create_connection(db, vault, "Northgate IT reporting", generate_certificate("demo"),
                                  client_id="11111111-2222-3333-4444-555555555555")
    ids = []
    for spec in TENANTS:
        t = Tenant(spec, now)
        tid = store.create_tenant(db, spec["name"], f"{spec['base']}.onmicrosoft.com", cid)
        db.execute("UPDATE tenants SET consented_at = ? WHERE id = ?", (iso(now - timedelta(days=200)), tid))
        collect(db, tid, t, first=True)
        backdate(db, tid, 35)
        collect(db, tid, t, first=False)
        if not ids:
            add_email(db, tid, t)
        ids.append(tid)
    for part, price in PRICES.items():
        db.execute("INSERT OR REPLACE INTO sku_prices (sku_part_number, monthly_price, updated_at) VALUES (?, ?, ?)",
                   (part, price, utcnow()))
    settings = dict(MSP)
    if (png := logo_png(MSP["brand_name"], MSP["brand_accent"])) and (uri := branding.logo_data_uri(png)):
        settings["brand_logo"] = uri
    for key, value in settings.items():
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    add_prospect(db)
    return ids
