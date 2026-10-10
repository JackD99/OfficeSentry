"""Three believable demo tenants, for demos, screenshots and reviewing report design.

    cd v2
    python scripts/demo_data.py --data-dir /tmp/officesentry-demo
    OFFICESENTRY_DATA_DIR=/tmp/officesentry-demo python -m officesentry web

No Microsoft tenant is needed: every collector runs against the recorded-style
Graph responses in ``tests/`` (as ``tests/seed.py`` does), with the fixtures'
people ("Ann", "bob@x.com") and domains swapped for each tenant's own, and
extra staff added so tables are a realistic length. Each tenant is collected
twice, five weeks apart, so "What changed" and the trends have history. MSP
branding (name, colour, contact and, with Pillow installed, a logo) and an
admin account are set up; the admin password is printed. With ``--public`` there is no admin, only the
read-only account every visitor of the public demo site becomes (``deploy/public/``).

Development only: it imports the test fixtures, which aren't in the Docker image.
"""

from __future__ import annotations

import argparse
import os
import random
import re
import secrets
import sys
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

V2 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(V2))

from werkzeug.security import generate_password_hash  # noqa: E402

from officesentry import store  # noqa: E402
from officesentry.certs import generate_certificate  # noqa: E402
from officesentry.db import utcnow  # noqa: E402
from tests import (  # noqa: E402  # noqa: E402
    adminactivitydata,
    devicedata,
    graphdata,
    groupdata,
    hybriddata,
    licencedata,
    sharepoint_data,
    tenantsettingsdata,
    usagedata,
)
from tests import test_collectors as fixture_users  # noqa: E402

FIXTURES = (graphdata, devicedata, groupdata, hybriddata, tenantsettingsdata, adminactivitydata, sharepoint_data,
            usagedata, licencedata, fixture_users)

FIRST = ["Olivia", "James", "Amelia", "Oliver", "Isla", "George", "Ava", "Harry", "Mia", "Noah", "Sophie", "Jack",
         "Grace", "Leo", "Freya", "Arthur", "Emily", "Oscar", "Lily", "Charlie", "Ella", "Henry", "Ruby", "Thomas",
         "Chloe", "William", "Evie", "Alfie", "Poppy", "Jacob", "Zara", "Samuel", "Imogen", "Daniel", "Maya",
         "Joseph", "Priya", "Aaron", "Hannah", "Rhys", "Megan", "Owen", "Niamh", "Callum", "Aisha", "Ethan"]
LAST = ["Smith", "Jones", "Taylor", "Brown", "Williams", "Wilson", "Johnson", "Davies", "Patel", "Robinson",
        "Wright", "Thompson", "Evans", "Walker", "White", "Roberts", "Green", "Hall", "Thomas", "Clarke", "Jackson",
        "Wood", "Harris", "Edwards", "Turner", "Martin", "Cooper", "Hill", "Ward", "Hughes", "Moore", "Clark",
        "King", "Harrison", "Lewis", "Baker", "Lee", "Allen", "Morris", "Khan", "Scott", "Watson", "Davis"]
DEPARTMENTS = ["Finance", "Operations", "Sales", "Marketing", "Customer Service", "HR", "Projects", "Reception"]
JOB_TITLES = {"Finance": "Accountant", "Operations": "Operations coordinator", "Sales": "Account manager",
              "Marketing": "Marketing executive", "Customer Service": "Customer service adviser",
              "HR": "HR adviser", "Projects": "Project manager", "Reception": "Receptionist"}
# Fixture account and group ids that appear in addresses ("u1@x.com"), and the outside domains the fixtures use.
FIXTURE_IDS = {"u1": "ann", "u2": "bob", "u5": "eve", "u6": "carl"}
GROUP_MAILS = {"g1": "sales", "g2": "project-phoenix", "g3": "marketing", "g4": "board", "g5": "provisioned",
               "m1": "finance-alerts", "d1": "info"}
OUTSIDE_DOMAINS = {"gmail.com": "homemail.example", "supplier.com": "supplier.example",
                   "other.com": "partner-firm.example", "partner.org": "consultants.example",
                   "parked.com": "parked.example"}

# Monthly prices (GBP) the MSP has entered, so licence waste shows money.
PRICES = {"SPB": 18.10, "O365_BUSINESS_PREMIUM": 10.30, "O365_BUSINESS_ESSENTIALS": 4.90, "EXCHANGESTANDARD": 3.30,
          "EXCHANGEENTERPRISE": 6.60, "ENTERPRISEPACK": 18.10}

TENANTS = [
    # name, domain base, top-level domain, staff, share of staff with MFA (first collection, now),
    # inactive staff, Secure Score (5 weeks ago, now), SharePoint storage used as a share of the tenant's quota
    # (5 weeks ago, now), the kind of SharePoint sites, mailboxes forwarding outside, the fixture people's new
    # names. Every domain is under .example, so nothing points at a real organisation.
    dict(name="Northwind Traders", base="northwindtraders", tld="example", staff=34, mfa=(0.62, 0.78), inactive=3,
         score=(38.0, 47.5), storage=(0.38, 0.41), forwards=1,
         people={"ann": "Ann Patel", "bob": "Robert Hughes", "carl": "Carl Jensen",
                "carol": "Carol Mendes", "erin": "Erin O'Neill", "eve": "Eve Sinclair",
                "dan": "Dan Whitaker", "mallory": "Mallory Price"}),
    dict(name="Fabrikam Dental Group", base="fabrikam", tld="example", staff=52, mfa=(0.41, 0.46), inactive=6,
         score=(31.0, 33.0), storage=(0.84, 0.88), sites="dental", forwards=2,
         people={"ann": "Anna Kowalski", "bob": "Ben Okafor", "carl": "Carl Svensson",
                "carol": "Caroline Reyes", "erin": "Erin Doyle", "eve": "Evelyn Shaw",
                "dan": "Daniel Frost", "mallory": "Mallory Quinn"}),
    dict(name="Contoso Legal LLP", base="contoso", tld="example", staff=17, mfa=(0.88, 0.94), inactive=1,
         score=(61.0, 68.0), storage=(0.27, 0.28), forwards=0,
         people={"ann": "Annabel Fraser", "bob": "Bobby Marsh", "carl": "Carl Ashworth",
                "carol": "Carol Bennett", "erin": "Erin Gallagher", "eve": "Eve Lambert",
                "dan": "Dan Redford", "mallory": "Mallory Hart"}),
]


def _local(full_name: str) -> str:
    first, _, last = full_name.partition(" ")
    return re.sub(r"[^a-z.]", "", f"{first}.{last}".lower())


def renamer(t: dict):
    """A function that rewrites fixture text for tenant ``t``: people, addresses and domains."""
    domain = f"{t['base']}.{t['tld']}"
    subs = [(re.compile(rf"\b{old}(?=@x\.|\.home@)"), _local(new)) for old, new in t["people"].items()]
    other = f"{t['base']}-uk.{t['tld']}"  # the fixtures' second domain
    subs += [(re.compile(rf"\b{uid}(?=@x\.)"), _local(t["people"][who])) for uid, who in FIXTURE_IDS.items()]
    subs += [(re.compile(rf"\b{gid}(?=@x\.)"), slug) for gid, slug in GROUP_MAILS.items()]
    subs += [(re.compile(r"\bbg(?=@x\.)"), "breakglass"), (re.compile(r"\bboss(?=@x\.)"), "office.manager"),
             (re.compile(r"\bx-com\b"), f"{t['base']}-{t['tld']}"),
             (re.compile(r"_x_com\b"), f"_{t['base']}_{t['tld']}"),
             (re.compile(r"(?<![\w-])x\.com\b"), domain),
             (re.compile(r"(?<![\w-])x\.co\.uk\b"), other),
             (re.compile(r"(?<![\w-])x(?=(\.onmicrosoft\.com|\.sharepoint\.com|-my\.sharepoint|-admin\.sharepoint))"),
              t["base"])]
    # Outside addresses in the fixtures (forwarding targets, guests) move to .example too.
    subs += [(re.compile(rf"\b{re.escape(real)}\b"), fake) for real, fake in OUTSIDE_DOMAINS.items()]
    subs += [(re.compile(rf"\b{old.capitalize()}\b"), new) for old, new in t["people"].items()]
    subs += [(re.compile(rf"\b{old.upper()}\b"), new.split()[-1].upper()) for old, new in t["people"].items()]
    subs += [(re.compile(r"^Breakglass$"), "Emergency access"), (re.compile(r"^Guest$"), "Sam Carter (guest)"),
             (re.compile(r"^OLD$"), "VISIOCLIENT")]  # the fixtures' lapsed subscription gets a real product name

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


class Renamed:
    """Swap every upper-case constant in the fixture modules for its renamed copy, and back afterwards."""

    def __init__(self, rename):
        self.rename, self.saved = rename, []

    def __enter__(self):
        for module in FIXTURES:
            for name, value in list(vars(module).items()):
                if name.isupper() and isinstance(value, (dict, list, tuple, str)):
                    self.saved.append((module, name, value))
                    setattr(module, name, self.rename(value))
        return self

    def __exit__(self, *exc):
        for module, name, value in self.saved:
            setattr(module, name, value)


def _methods(i: int) -> list[str]:
    """A realistic mix: mostly the Authenticator app, some Windows Hello or passkeys, a few on text messages."""
    if i % 5 == 0:
        return ["windowsHelloForBusiness", "microsoftAuthenticatorPush"]
    if i % 11 == 4:
        return ["passKeyDeviceBound", "microsoftAuthenticatorPush"]
    if i % 7 == 2:
        return ["mobilePhone"]
    return ["microsoftAuthenticatorPush", "softwareOneTimePasscode"]


def staff(t: dict, rng: random.Random, now: datetime, mfa_share: float, joined_recently: bool):
    """Extra member accounts (beyond the fixture's few), their MFA registration and Microsoft 365 use."""
    users, registration, active, apps = [], [], [], []
    names = set(t["people"].values())
    n = t["staff"] - (0 if joined_recently else 2)
    for i in range(n):
        while (name := f"{rng.choice(FIRST)} {rng.choice(LAST)}") in names:
            pass
        names.add(name)
        upn = f"{_local(name)}@{t['base']}.{t['tld']}"
        uid = f"d{i:03d}"
        inactive = i < t["inactive"]
        last = now - timedelta(days=rng.randint(95, 300) if inactive else rng.choice([0, 0, 0, 1, 1, 2, 3, 6, 12]),
                               hours=rng.randint(0, 9))
        sku = "sku-bp" if i % 5 else "sku-basic"
        # The first person in each department heads it and reports to Ann; everyone else reports to their head.
        department = DEPARTMENTS[i % len(DEPARTMENTS)]
        head = users[i % len(DEPARTMENTS)] if i >= len(DEPARTMENTS) else None
        manager = ({"@odata.type": "#microsoft.graph.user", "id": head["id"], "displayName": head["displayName"],
                    "userPrincipalName": head["userPrincipalName"], "accountEnabled": True} if head else
                   {"@odata.type": "#microsoft.graph.user", "id": "u1", "displayName": t["people"]["ann"],
                    "accountEnabled": True})
        users.append({"id": uid, "displayName": name, "userPrincipalName": upn, "mail": upn, "userType": "Member",
                      "accountEnabled": True, "assignedLicenses": [{"skuId": sku}],
                      "jobTitle": JOB_TITLES[department] if head else f"Head of {department}",
                      "department": department, "manager": manager,
                      "createdDateTime": (now - timedelta(days=30 + 37 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "signInActivity": {"lastSignInDateTime": last.strftime("%Y-%m-%dT%H:%M:%SZ")}})
        registered = rng.random() < mfa_share and not inactive
        registration.append({"id": uid, "userPrincipalName": upn, "userDisplayName": name, "userType": "member",
                             "isAdmin": False, "isMfaRegistered": registered, "isMfaCapable": registered,
                             "methodsRegistered": _methods(i) if registered else []})
        product = "MICROSOFT 365 BUSINESS PREMIUM" if sku == "sku-bp" else "MICROSOFT 365 BUSINESS BASIC"
        quiet = None if inactive else rng.choice([0, 1, 2, 4])
        mail_only = not inactive and i % 7 == 3  # Business Premium used for email alone: right-sizing finds them
        active.append(usagedata.active_user(upn, [product], name=name, exchange=quiet,
                                            teams=None if mail_only else quiet,
                                            onedrive=None if inactive or mail_only else rng.choice([1, 3, 9, None])))
        if inactive:
            apps.append(usagedata.app_user(upn))
        elif mail_only or sku == "sku-basic":
            apps.append(usagedata.app_user(upn, quiet, outlook=("Web", "Mobile")))
        else:
            desk = ("Mac",) if i % 9 == 2 else ("Windows",)
            apps.append(usagedata.app_user(upn, quiet, outlook=desk + ("Mobile",), word=desk, excel=desk,
                                           teams=desk + ("Mobile",)))
    return users, registration, active, apps


def skus(t: dict, assigned_bp: int, assigned_basic: int) -> list[dict]:
    bp = dict(fixture_users.SKUS[0], prepaidUnits={"enabled": assigned_bp + 3}, consumedUnits=assigned_bp)
    basic = {"skuId": "sku-basic", "skuPartNumber": "O365_BUSINESS_ESSENTIALS", "appliesTo": "User",
             "capabilityStatus": "Enabled", "prepaidUnits": {"enabled": assigned_basic + 2},
             "consumedUnits": assigned_basic,
             "servicePlans": [{"servicePlanName": "EXCHANGE_S_STANDARD", "provisioningStatus": "Success"}]}
    return [bp, basic, fixture_users.SKUS[1]]


def secure_scores(start: float, end: float, now: datetime, days: int = 70) -> list[dict]:
    rows = []
    for d in range(days, -1, -1):
        progress = (days - d) / days
        score = round(start + (end - start) * progress ** 1.6, 1)
        rows.append({"createdDateTime": (now - timedelta(days=d)).strftime("%Y-%m-%dT00:00:00Z"),
                     "currentScore": score, "maxScore": 100.0,
                     "controlScores": graphdata.SECURE_SCORES_HISTORY[0]["controlScores"]})
    return list(reversed(rows))


# -- Mailboxes, devices and SharePoint at a realistic size ------------------------------------
# The fixtures have two mailboxes, five devices and one SharePoint site; these build the same shapes for
# every account, so the detail reports and the SharePoint storage report look like a real tenant's.

GB, MB, TB = 1024 ** 3, 1024 ** 2, 1024 ** 4
DENTAL_SITES = [  # slug, title, share of the space used, recycle bin (GB), site template
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
FOLDERS = {
    "imaging": ["X-rays 2019", "X-rays 2020", "CBCT scans", "Intraoral photos", "Referral images"],
    "patient-records": ["Consent forms", "Treatment plans", "Lab dockets", "Correspondence"],
    "practice": ["Rotas", "Policies", "Compliance", "Equipment manuals"],
    "clients": ["Client files", "Engagement letters", "Year end", "Payroll"],
}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _upn(t: dict, who: str) -> str:
    return f"{_local(t['people'][who])}@{t['base']}.{t['tld']}"


def exchange(t: dict, users: list[dict], first: bool, rng: random.Random) -> dict:
    """Get-* results for every licensed account plus shared mailboxes: forwarding, delegates, sizes, protocols."""
    domain, quota = f"{t['base']}.{t['tld']}", "49.5 GB (53,150,220,288 bytes)"
    boxes, stats, cas, perms, send_as = [], {}, [], {}, []
    people = [u for u in users if u["userType"] == "Member" and u.get("assignedLicenses")]
    forwards = 0 if first else t.get("forwards", 0)
    for i, u in enumerate(people):
        guid, upn = f"g-{u['id']}", u["userPrincipalName"]
        box = {"ExternalDirectoryObjectId": u["id"], "Guid": guid, "UserPrincipalName": upn,
               "PrimarySmtpAddress": upn, "DisplayName": u["displayName"], "Name": u["id"],
               "Alias": upn.split("@")[0], "RecipientTypeDetails": "UserMailbox", "GrantSendOnBehalfTo": [],
               "ProhibitSendQuota": quota, "ArchiveStatus": "Active" if i % 4 == 0 else "None"}
        if u["id"] == "u1":
            box.update(ForwardingSmtpAddress=f"smtp:{upn.split('@')[0]}@homemail.example",
                       DeliverToMailboxAndForward=True)
        elif forwards and i % 11 == 6:
            forwards -= 1
            box.update(ForwardingSmtpAddress=f"smtp:{upn.split('@')[0]}@homemail.example",
                       DeliverToMailboxAndForward=False)
        boxes.append(box)
        size = 47.2 if i == 3 else 45.6 if i == 9 else rng.uniform(1.5, 32)
        stats[guid] = [{"TotalItemSize": f"{size:.1f} GB ({int(size * GB):,} bytes)",
                        "TotalDeletedItemSize": f"{size / 20:.1f} GB ({int(size * GB / 20):,} bytes)",
                        "ItemCount": int(size * 2400)}]
        perms[guid] = [{"User": "NT AUTHORITY\\SELF", "AccessRights": ["FullAccess"], "IsInherited": False}]
        cas.append({"ExternalDirectoryObjectId": u["id"], "Guid": guid, "PrimarySmtpAddress": upn,
                    "SmtpClientAuthenticationDisabled": None if i % 8 == 1 else True,
                    "PopEnabled": i % 10 == 3, "ImapEnabled": i % 5 == 2})
    staff_upns = [u["userPrincipalName"] for u in people if u["id"].startswith("d") and u["accountEnabled"]]
    for sid, alias, display, kind, size in (("u7", "info", "Info", "SharedMailbox", 6.4),
                                            ("x-rec", "reception", "Reception", "SharedMailbox", 18.9),
                                            ("x-acc", "accounts", "Accounts", "SharedMailbox", 44.8),
                                            ("x-room", "boardroom", "Boardroom", "RoomMailbox", 0.1)):
        guid, addr = f"g-{sid}", f"{alias}@{domain}"
        boxes.append({"ExternalDirectoryObjectId": sid, "Guid": guid, "UserPrincipalName": addr,
                      "PrimarySmtpAddress": addr, "DisplayName": display, "Name": display, "Alias": alias,
                      "RecipientTypeDetails": kind, "ProhibitSendQuota": quota,
                      "GrantSendOnBehalfTo": [t["people"]["ann"]] if alias == "info" else []})
        stats[guid] = [{"TotalItemSize": f"{size:.1f} GB ({int(size * GB):,} bytes)",
                        "TotalDeletedItemSize": "0 B (0 bytes)", "ItemCount": int(size * 3000)}]
        if kind == "RoomMailbox":
            continue
        who = [_upn(t, "ann")] + rng.sample(staff_upns, 3)
        if alias == "accounts":  # a leaver and an outsider still have access
            who += [_upn(t, "bob"), "sam.carter@consultants.example"]
        perms[guid] = [{"User": w, "AccessRights": ["FullAccess"], "IsInherited": False} for w in who]
        send_as += [{"Identity": display, "Trustee": w, "AccessRights": ["SendAs"]} for w in who[:2]]
        cas.append({"ExternalDirectoryObjectId": sid, "Guid": guid, "PrimarySmtpAddress": addr,
                    "SmtpClientAuthenticationDisabled": None if alias == "reception" else True,
                    "PopEnabled": alias == "info", "ImapEnabled": alias == "info"})
    return {**graphdata.EXCHANGE, "Get-AcceptedDomain": [
        {"DomainName": domain, "DomainType": "Authoritative", "Default": True}],
        "Get-Mailbox": boxes, "Get-MailboxPermission": perms, "Get-MailboxStatistics": stats,
        "Get-CASMailbox": cas, "Get-RecipientPermission": send_as}


def inbox_rules(users: list[dict]) -> dict:
    rules = dict(graphdata.INBOX_RULES)
    for i, u in enumerate(u for u in users if u["id"].startswith("d")):
        if i % 4 == 1:
            rules[u["id"]] = [{"id": f"r-{u['id']}", "displayName": "Newsletters", "isEnabled": True, "sequence": 1,
                               "conditions": {"senderContains": ["newsletter"]},
                               "actions": {"moveToFolder": "fid-news", "stopProcessingRules": False}}]
        if i == 7:
            rules[u["id"]] = [{"id": f"r-{u['id']}-fwd", "displayName": "Invoices to bookkeeper", "isEnabled": True,
                               "sequence": 1, "conditions": {"subjectContains": ["invoice"]},
                               "actions": {"forwardTo": [{"emailAddress": {
                                   "address": "bookkeeping@ledgerline.example"}}]}}]
    return rules


def folders(rules: dict) -> dict:
    """Get-InboxRule's answer for the rules that move mail, which is where folder names come from."""
    names = {"fid-rss": "RSS Feeds", "fid-news": "Newsletters"}
    return {uid: [{"Name": r["displayName"], "RuleIdentity": i, "MoveToFolder": names[r["actions"]["moveToFolder"]]}
                  for i, r in enumerate(rs) if r["actions"].get("moveToFolder")] for uid, rs in rules.items()}


def devices(users: list[dict], rng: random.Random) -> tuple[list[dict], list[dict]]:
    """A Windows PC per person: mostly current builds, a few on Windows 10, some not checking in or unencrypted."""
    entra, managed = list(devicedata.ENTRA_DEVICES), list(devicedata.MANAGED_DEVICES)
    builds = ["10.0.26200.6899", "10.0.26100.6584", "10.0.26200.6584", "10.0.26100.4946", "10.0.19045.6332"]
    models = [("Dell", "Latitude 7450"), ("Lenovo", "ThinkPad T14 Gen 5"), ("HP", "EliteBook 840 G10"),
              ("Microsoft", "Surface Laptop 6"), ("Dell", "OptiPlex 7010")]
    for i, u in enumerate(u for u in users if u["id"].startswith("d") and u["accountEnabled"]):
        name = f"{'DESK' if i % 3 == 0 else 'LAPTOP'}-{u['displayName'].split()[-1].upper()[:8]}{i:02d}"
        build = builds[4 if i % 9 == 4 else rng.randrange(4)]
        sync = 38 if i % 13 == 12 else 0
        healthy = i % 11 != 7
        make, model = models[i % len(models)]
        entra.append({"id": f"ed-{u['id']}", "deviceId": f"dev-{u['id']}", "displayName": name,
                      "operatingSystem": "Windows", "operatingSystemVersion": build, "trustType": "AzureAd",
                      "isManaged": True, "isCompliant": healthy and not sync, "accountEnabled": True,
                      "approximateLastSignInDateTime": devicedata.days_ago(sync + rng.randint(0, 2)),
                      "registrationDateTime": devicedata.days_ago(200 + 30 * i)})
        managed.append({"id": f"md-{u['id']}", "deviceName": name, "operatingSystem": "Windows", "osVersion": build,
                        "complianceState": "compliant" if healthy and not sync else "noncompliant",
                        "isEncrypted": healthy, "lastSyncDateTime": devicedata.days_ago(sync),
                        "enrolledDateTime": devicedata.days_ago(200 + 30 * i),
                        "userPrincipalName": u["userPrincipalName"], "managedDeviceOwnerType": "company",
                        "manufacturer": make, "model": model, "jailBroken": "Unknown",
                        "azureADDeviceId": f"dev-{u['id']}"})
    return entra, managed


def _list_item(t: dict, now: datetime, ref: str, *, total: int, current: int | None = None, version: str = "1.0",
               folder: bool = False, files: int = 0, modified: int = 10, by: str | None = None) -> dict:
    fields = {"FileLeafRef": ref.rsplit("/", 1)[-1], "FileRef": ref, "FSObjType": "1" if folder else "0",
              "SMTotalSize": str(total),
              "SMTotalFileStreamSize": str(0 if folder else (total if current is None else current)),
              "SMTotalFileCount": str(files), "_UIVersionString": version}
    who = by or _upn(t, "ann")
    name = next((n for k, n in t["people"].items() if _upn(t, k) == who), who)
    return {"id": str(zlib.crc32(ref.encode())),
            "webUrl": f"https://{t['base']}.sharepoint.com" + ref.replace(" ", "%20"),
            "lastModifiedDateTime": _iso(now - timedelta(days=modified, hours=4)),
            "lastModifiedBy": {"user": {"email": who, "displayName": name}}, "fields": fields}


def _library(t: dict, now: datetime, slug: str, size: int, rng: random.Random) -> list[dict]:
    """Folders and files adding up to ``size``: big files nobody opens, files with long version histories."""
    root = f"/sites/{slug}/Shared Documents"
    authors = [_upn(t, k) for k in ("ann", "carl", "eve", "carol")]
    out, left = [], size - (int(9.6 * GB) + 31 * GB if slug == "practice" else 0)  # the two files added below
    folders = FOLDERS.get(slug, ["Projects", "Archive", "Templates", "Shared", "Scans"])
    for n, folder in enumerate(folders):
        part = left * (0.42 if n < len(folders) - 1 else 1.0)
        left -= part
        path = f"{root}/{folder}"
        out.append(_list_item(t, now, path, total=int(part), folder=True, files=rng.randint(400, 9000),
                              modified=rng.randint(1, 60)))
        files = [("archive.zip", 0.18, "1.0", 900), ("export.pst", 0.07, "1.0", 1100)] if n % 2 == 0 else \
            [("tracker.xlsx", 0.04, "640.0", 2), ("planner.docx", 0.02, "212.0", 5)]
        for suffix, share, version, age in files:
            total = int(part * share)
            current = total if version == "1.0" else max(int(total / float(version) * 3), 8 * MB)
            out.append(_list_item(t, now, f"{path}/{folder} {suffix}", total=total, current=current,
                                  version=version, modified=age, by=rng.choice(authors)))
        # The rest of the folder: ordinary files, so the libraries add up to what the sites use.
        rest = part * (1 - sum(share for _, share, _, _ in files)) / 12
        for k in range(12):
            stem, ext = (("Scans", ".zip"), ("Photos", ".zip"), ("Recording", ".mp4"), ("Export", ".zip"))[k % 4]
            year = int(folder[-4:]) if folder[-4:].isdigit() else 2019 + k % 6
            name = f"{stem} {year}-{1 + k % 12:02d}{ext}"
            size = int(rest * (0.6 + 0.8 * k / 11))
            out.append(_list_item(t, now, f"{path}/{name}", total=size, current=int(size * 0.93),
                                  version=f"{2 + k % 5}.0", modified=rng.randint(3, 1500), by=rng.choice(authors)))
    if slug == "practice":
        out.append(_list_item(t, now, f"{root}/Appointment book.xlsx", total=int(9.6 * GB), current=14 * MB,
                              version="1184.0", modified=0, by=_upn(t, "carl")))
        out.append(_list_item(t, now, f"{root}/Practice database backup.bak", total=31 * GB, modified=820))
    return out


def sharepoint(t: dict, users: list[dict], tenant_skus: list[dict], now: datetime, first: bool,
               rng: random.Random) -> dict:
    """sharepoint_data constants for a tenant using ``t["storage"]`` of its quota, growing over the 5 weeks."""
    from officesentry.storage import tenant_quota

    host = f"https://{t['base']}.sharepoint.com"
    then, current = t.get("storage", (0.3, 0.32))
    quota = tenant_quota(tenant_skus).total
    total = int(quota * (then if first else current))
    sites, drives, quotas, lists, items, recycle, usage, delta = [], {}, {}, {}, {}, {}, [], {}
    for slug, title, share, bin_gb, template in DENTAL_SITES if t.get("sites") == "dental" else OFFICE_SITES:
        sid = "site-fin" if slug == "finance" else f"site-{slug}"
        url, size = f"{host}/sites/{slug}", int(total * share)
        sites.append({"id": sid, "displayName": title, "webUrl": url})
        drives[sid] = [{"id": f"drv-{slug}", "name": "Documents", "webUrl": f"{url}/Shared Documents",
                        "driveType": "documentLibrary"}]
        delta[f"drv-{slug}"] = []
        quotas[sid] = {"used": size, "deleted": bin_gb * GB, "total": 27487790694400, "state": "normal"}
        lists[sid] = [{"id": f"lst-{slug}", "name": "Shared Documents", "displayName": "Documents",
                       "webUrl": f"{url}/Shared%20Documents", "list": {"template": "documentLibrary", "hidden": False}}]
        hold = int(size * 0.12) if slug in ("patient-records", "finance", "clients") else 0
        if hold:
            lists[sid].append({"id": f"lst-{slug}-hold", "name": "PreservationHoldLibrary",
                               "displayName": "Preservation Hold Library", "webUrl": f"{url}/PreservationHoldLibrary",
                               "list": {"template": "documentLibrary", "hidden": False}, "system": {}})
            items[f"lst-{slug}-hold"] = [
                _list_item(t, now, f"/sites/{slug}/PreservationHoldLibrary/{name}", total=hold // parts, modified=400)
                for name, parts in (("Contracts archive_v12.zip", 2), ("Board pack 2023_v8.pdf", 3))]
        # A site's storage counts its recycle bin too, so the files leave room for it and the parts add up.
        items[f"lst-{slug}"] = _library(t, now, slug, size - hold - bin_gb * GB, rng)
        recycle[sid] = [] if not bin_gb else [
            {"id": f"rb-{slug}-{n}", "name": name, "size": int(bin_gb * GB * part),
             "deletedDateTime": _iso(now - timedelta(days=days)),
             "deletedFromLocation": f"sites/{slug}/Shared Documents/{folder}"}
            for n, (name, part, days, folder) in enumerate((
                ("Scanner exports 2022.zip", 0.55, 12, "Scans"), ("Old camera images", 0.3, 33, "Images"),
                ("Copy of Copy of Rota.xlsx", 0.15, 4, "Rotas")))]
        usage.append(sharepoint_data._site(
            url, template, activity=410 if slug == "old-projects" else rng.choice([0, 1, 2, 4]), storage=size,
            anyone=2 if slug == "marketing" else 0, guest=1 if slug in ("finance", "clients") else 0, company=3,
            owner=t["people"]["ann"]))
    # Shared files: keep the fixture's Finance library and OneDrive (renamed) so sharing links show up.
    drives["site-fin"] = sharepoint_data.DRIVES["site-fin"]
    delta.update({k: sharepoint_data.DELTA[k] for k in ("drv-fin", "drv-ann")})
    sites.append(sharepoint_data.SITES[1])
    drives[sharepoint_data.SITES[1]["id"]] = sharepoint_data.DRIVES[sharepoint_data.SITES[1]["id"]]
    growth = (current - then) * quota / 35
    history = [{"reportRefreshDate": sharepoint_data.days_ago(2), "siteType": "All",
                "reportDate": sharepoint_data.days_ago(2 + d), "storageUsedInBytes": int(total - d * growth),
                "reportPeriod": "180"} for d in range(0, 180, 5)]
    onedrives = [sharepoint_data._drive(u["userPrincipalName"], int(rng.uniform(0.5, 60) * GB),
                                        activity=rng.choice([1, 2, 4]))
                 for u in users if u["userType"] == "Member" and u.get("assignedLicenses")]
    return {"SITES": sites, "DRIVES": drives, "SITE_QUOTA": quotas, "LISTS": lists,
            "LIST_ITEMS": {**items, "list-ann": sharepoint_data.LIST_ITEMS["list-ann"]}, "RECYCLE_BIN": recycle,
            "SITE_USAGE": usage, "SITE_STORAGE": history, "ONEDRIVE_USAGE": onedrives, "DELTA": delta}


class Swapped:
    """Set module constants for the duration of a collection, and put them back afterwards."""

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


def collect(db, tenant_id: int, t: dict, now: datetime, first: bool) -> None:
    from tests.seed import seed_tenant

    with Renamed(renamer(t)):
        # The same seed both times: the same people, two of whom join after the first collection.
        rng = random.Random(t["name"])  # noqa: S311 (demo data, not security)
        users, registration, active, apps = staff(t, rng, now, t["mfa"][0 if first else 1], joined_recently=not first)
        everyone = fixture_users.USERS + users
        bp = sum(1 for u in everyone if {"skuId": "sku-bp"} in u.get("assignedLicenses", []))
        basic = sum(1 for u in everyone if {"skuId": "sku-basic"} in u.get("assignedLicenses", []))
        tenant_skus = skus(t, bp, basic)
        rng = random.Random(t["name"] + "extras")  # noqa: S311
        entra, managed = devices(everyone, rng)
        rules = inbox_rules(everyone)
        extras = {graphdata: {"EXCHANGE": {**exchange(t, everyone, first, rng), "Get-InboxRule": folders(rules)}},
                  devicedata: {"ENTRA_DEVICES": entra, "MANAGED_DEVICES": managed},
                  sharepoint_data: sharepoint(t, everyone, tenant_skus, now, first, rng)}
        with Swapped(extras):
            seed_tenant(db, tenant_id, dns=graphdata.fake_dns, users=everyone, skus=tenant_skus,
                        registration=graphdata.REGISTRATION_DETAILS + registration,
                        secure_scores=secure_scores(t["score"][0] - 4, t["score"][0], now) if first
                        else secure_scores(t["score"][0] - 4, t["score"][1], now, 105),
                        usage_kw={"active": usagedata.ACTIVE_USERS + active, "apps": usagedata.APP_USERS + apps},
                        inbox_rules=rules)


def activity_history(db, tenant_id: int, t: dict, now: datetime, days: int = 120) -> None:
    """Four months of sign-ins and a few admin changes in the activity history, so its page, each person's
    sign-ins by month and the Admin activity report have something to show (the fixtures hold a few days)."""
    from officesentry import events
    from officesentry.snapshot import TenantSnapshot

    rng = random.Random(t["name"] + "history")  # noqa: S311 (demo data, not security)
    snap = TenantSnapshot(db, store.get_tenant(db, tenant_id))
    people = [u for u in snap.objects("users", "user") or []
              if u.get("accountEnabled") is not False and u.get("userType") != "Guest" and u.get("userPrincipalName")]
    mfa_share = t["mfa"][0]
    rows = []
    for back in range(days, 0, -1):
        day = (now - timedelta(days=back)).replace(hour=0, minute=0, second=0, microsecond=0)
        weekend = day.weekday() >= 5
        for i, person in enumerate(people):
            if rng.random() > (0.12 if weekend else 0.85):
                continue
            uid = person.get("id") or person["_id"]
            for _ in range(rng.randint(1, 4)):
                when = day + timedelta(hours=rng.randint(7, 18), minutes=rng.randint(0, 59))
                code = rng.choices([0, 50074, 50126, 50140], [86, 7, 4, 3])[0]
                country = "GB" if rng.random() > 0.03 else rng.choice(["FR", "IE", "US"])
                rows.append(graphdata._signin(len(rows), _iso(when), person["userPrincipalName"], uid, code,
                                              app=rng.choice(["Microsoft Teams", "Office 365 Exchange Online",
                                                              "Office 365 SharePoint Online", "Microsoft Office"]),
                                              ip=f"203.0.113.{10 + i % 40}", country=country,
                                              mfa=rng.random() < mfa_share))
    for n, row in enumerate(rows):
        row["id"] = f"demo-{tenant_id}-{n}"
    base, key = events.root(db), events.tenant_key(db, tenant_id, create=True)
    events.add_signins(base, key, rows)
    events.rebuild_daily(base, key, {r["createdDateTime"][:10] for r in rows})
    with Renamed(renamer(t)):
        older = ((adminactivitydata.GLOBAL_ADMIN_GRANT, 64), (adminactivitydata.CA_DISABLED, 41),
                 (adminactivitydata.SECRET_ADDED, 12), (adminactivitydata.CA_ADDED, 90))
        events.add_audits(base, key, [{**event, "id": f"demo-audit-{tenant_id}-{n}",
                                       "activityDateTime": _iso(now - timedelta(days=back))}
                                      for n, (event, back) in enumerate(older)])
    start = int((now - timedelta(days=days)).timestamp())
    for source in ("signins", "audits"):
        cov = events.coverage(db, tenant_id, source)
        cov.since = min(cov.since or start, start)
        cov.read_to = cov.read_to or int(now.timestamp())
        events.save_coverage(db, tenant_id, cov)


def backdate(db, tenant_id: int, days: int) -> None:
    for table, cols in (("runs", ("created_at", "started_at", "finished_at")), ("metrics", ("captured_at",)),
                        ("findings", ("first_seen", "last_seen"))):
        sets = ", ".join(f"{c} = strftime('%Y-%m-%dT%H:%M:%SZ', {c}, '-{days} days')" for c in cols)
        db.execute(f"UPDATE {table} SET {sets} WHERE tenant_id = ?", (tenant_id,))


def logo_png(name: str, accent: str) -> bytes | None:
    """A simple wordmark, if Pillow is installed."""
    try:
        from io import BytesIO

        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    font = V2 / "officesentry" / "web" / "static" / "fonts" / "Inter-Bold.ttf"
    img = Image.new("RGBA", (720, 180), (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)
    rgb = tuple(int(accent[i:i + 2], 16) for i in (1, 3, 5))
    draw.rounded_rectangle((10, 30, 130, 150), radius=26, fill=(*rgb, 255))
    draw.polygon([(40, 120), (70, 60), (100, 120)], fill=(255, 255, 255, 255))
    draw.text((160, 42), name, font=ImageFont.truetype(str(font), 78), fill=(16, 42, 67, 255))
    out = BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def groups(db, ids: list[int]) -> None:
    """A few tenant groups, so the group picker and the Tenant groups page have something to show."""
    from officesentry import tenant_groups

    north, fabrikam, contoso = ids[:3]
    for name, category, description, members in (
            ("Healthcare", "Sector", "Clinics and practices: patient data, so insurers ask more.", [fabrikam]),
            ("Professional services", "Sector", "", [north, contoso]),
            ("Managed+", "Tier", "Monthly review meeting and quarterly projects.", [fabrikam, contoso])):
        gid = tenant_groups.create(db, {"name": name, "category": category, "description": description}, "demo")
        tenant_groups.set_members(db, gid, members)


def populate(db, vault, *, msp: str = "Harbour IT", accent: str = "#0d7a5f",
             contact: str = "help@harbourit.example · 0161 496 0000") -> list[int]:
    """Add the demo tenants, their history, prices and MSP branding to a migrated database."""
    from officesentry.reports import branding

    now = datetime.now(UTC)
    cid = store.create_connection(db, vault, "Demo app", generate_certificate("demo"),
                                  client_id="11111111-2222-3333-4444-555555555555")
    ids = []
    for t in TENANTS:
        tid = store.create_tenant(db, t["name"], f"{t['base']}.onmicrosoft.com", cid)
        db.execute("UPDATE tenants SET consented_at = ? WHERE id = ?", (utcnow(), tid))
        collect(db, tid, t, now, first=True)
        backdate(db, tid, 35)
        activity_history(db, tid, t, now)
        collect(db, tid, t, now, first=False)
        ids.append(tid)
    groups(db, ids)
    for part, price in PRICES.items():
        db.execute("INSERT OR REPLACE INTO sku_prices (sku_part_number, monthly_price, updated_at) VALUES (?, ?, ?)",
                   (part, price, utcnow()))
    settings = {"brand_name": msp, "brand_accent": accent, "brand_contact": contact}
    if (png := logo_png(msp, accent)) and (uri := branding.logo_data_uri(png)):
        settings["brand_logo"] = uri
    for key, value in settings.items():
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    return ids


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", required=True, type=Path, help="a new, empty data directory")
    parser.add_argument("--admin", default="demo", help="admin username to create (default: demo)")
    parser.add_argument("--password", default=os.environ.get("DEMO_PASSWORD") or None,
                        help="the admin's password (default: $DEMO_PASSWORD, else a new random one)")
    parser.add_argument("--public", action="store_true",
                        help="for the public demo site: no admin, just the read-only analyst every visitor becomes "
                             "(with OFFICESENTRY_PUBLIC_DEMO=1; see officesentry/web/public_demo.py)")
    args = parser.parse_args()

    from officesentry.config import load_config
    from officesentry.crypto import Vault
    from officesentry.db import connect, migrate

    cfg = load_config(data_dir=args.data_dir)
    db = connect(cfg.db_path)
    migrate(db)
    if db.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]:
        sys.exit(f"{args.data_dir} already has tenants; use a new data directory.")
    ids = populate(db, Vault(cfg.encryption_keys))
    if args.public:
        from officesentry.web import public_demo

        # Nobody signs in with a password: visitors are this account without one (public_demo.py).
        db.execute("INSERT INTO users (username, password_hash, role, all_tenants, created_at)"
                   " VALUES (?, ?, 'analyst', 1, ?)",
                   (public_demo.USERNAME, generate_password_hash(secrets.token_urlsafe(32)), utcnow()))
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')", (public_demo.MARKER,))
        print(f"Added {len(ids)} demo tenants to {cfg.db_path} for the public demo site")
        return
    password = args.password or secrets.token_urlsafe(12)
    db.execute("INSERT INTO users (username, password_hash, role, all_tenants, created_at)"
               " VALUES (?, ?, 'admin', 1, ?)",
               (args.admin, generate_password_hash(password), utcnow()))
    print(f"Added {len(ids)} demo tenants to {cfg.db_path}")
    print(f"Sign in as {args.admin} / {password}")
    print(f"Start the portal: OFFICESENTRY_DATA_DIR={args.data_dir} python -m officesentry web")


if __name__ == "__main__":
    main()
