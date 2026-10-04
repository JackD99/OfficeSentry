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
admin account are set up; the admin password is printed.

Development only: it imports the test fixtures, which aren't in the Docker image.
"""

from __future__ import annotations

import argparse
import random
import re
import secrets
import sys
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
    sharepoint_data,
    tenantsettingsdata,
    usagedata,
)
from tests import test_collectors as fixture_users  # noqa: E402

FIXTURES = (graphdata, devicedata, groupdata, hybriddata, tenantsettingsdata, adminactivitydata, sharepoint_data,
            usagedata, fixture_users)

FIRST = ["Olivia", "James", "Amelia", "Oliver", "Isla", "George", "Ava", "Harry", "Mia", "Noah", "Sophie", "Jack",
         "Grace", "Leo", "Freya", "Arthur", "Emily", "Oscar", "Lily", "Charlie", "Ella", "Henry", "Ruby", "Thomas",
         "Chloe", "William", "Evie", "Alfie", "Poppy", "Jacob", "Zara", "Samuel", "Imogen", "Daniel", "Maya",
         "Joseph", "Priya", "Aaron", "Hannah", "Rhys", "Megan", "Owen", "Niamh", "Callum", "Aisha", "Ethan"]
LAST = ["Smith", "Jones", "Taylor", "Brown", "Williams", "Wilson", "Johnson", "Davies", "Patel", "Robinson",
        "Wright", "Thompson", "Evans", "Walker", "White", "Roberts", "Green", "Hall", "Thomas", "Clarke", "Jackson",
        "Wood", "Harris", "Edwards", "Turner", "Martin", "Cooper", "Hill", "Ward", "Hughes", "Moore", "Clark",
        "King", "Harrison", "Lewis", "Baker", "Lee", "Allen", "Morris", "Khan", "Scott", "Watson", "Davis"]
DEPARTMENTS = ["Finance", "Operations", "Sales", "Marketing", "Customer Service", "HR", "Projects", "Reception"]

# Monthly prices (GBP) the MSP has entered, so licence waste shows money.
PRICES = {"SPB": 18.10, "O365_BUSINESS_PREMIUM": 10.30, "O365_BUSINESS_ESSENTIALS": 4.90, "EXCHANGESTANDARD": 3.30}

TENANTS = [
    # name, domain base, top-level domain, staff, share of staff with MFA (first collection, now),
    # inactive staff, extra SKUs, Secure Score (5 weeks ago, now), the fixture people's new names
    dict(name="Northwind Traders", base="northwindtraders", tld="com", staff=34, mfa=(0.62, 0.78), inactive=3,
         score=(38.0, 47.5), people={"ann": "Ann Patel", "bob": "Robert Hughes", "carl": "Carl Jensen",
                                     "carol": "Carol Mendes", "erin": "Erin O'Neill", "eve": "Eve Sinclair",
                                     "dan": "Dan Whitaker", "mallory": "Mallory Price"}),
    dict(name="Fabrikam Dental Group", base="fabrikam", tld="com", staff=52, mfa=(0.41, 0.46), inactive=6,
         score=(31.0, 33.0), people={"ann": "Anna Kowalski", "bob": "Ben Okafor", "carl": "Carl Svensson",
                                     "carol": "Caroline Reyes", "erin": "Erin Doyle", "eve": "Evelyn Shaw",
                                     "dan": "Daniel Frost", "mallory": "Mallory Quinn"}),
    dict(name="Contoso Legal LLP", base="contoso", tld="co.uk", staff=17, mfa=(0.88, 0.94), inactive=1,
         score=(61.0, 68.0), people={"ann": "Annabel Fraser", "bob": "Bobby Marsh", "carl": "Carl Ashworth",
                                     "carol": "Carol Bennett", "erin": "Erin Gallagher", "eve": "Eve Lambert",
                                     "dan": "Dan Redford", "mallory": "Mallory Hart"}),
]


def _local(full_name: str) -> str:
    first, _, last = full_name.partition(" ")
    return re.sub(r"[^a-z.]", "", f"{first}.{last}".lower())


def renamer(t: dict):
    """A function that rewrites fixture text for tenant ``t``: people, addresses and domains."""
    domain = f"{t['base']}.{t['tld']}"
    subs = [(re.compile(rf"\b{old}(?=@x\.)"), _local(new)) for old, new in t["people"].items()]
    other = f"{t['base']}.{'co.uk' if t['tld'] == 'com' else 'com'}"  # the fixtures' second domain
    subs += [(re.compile(r"\bbg(?=@x\.)"), "breakglass"),
             (re.compile(r"(?<![\w-])x\.com\b"), domain),
             (re.compile(r"(?<![\w-])x\.co\.uk\b"), other),
             (re.compile(r"(?<![\w-])x(?=(\.onmicrosoft\.com|\.sharepoint\.com|-my\.sharepoint|-admin\.sharepoint))"),
              t["base"])]
    subs += [(re.compile(rf"\b{old.capitalize()}\b"), new) for old, new in t["people"].items()]
    subs += [(re.compile(r"^Breakglass$"), "Emergency access"), (re.compile(r"^Guest$"), "Sam Carter (guest)")]

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


def staff(t: dict, rng: random.Random, now: datetime, mfa_share: float, joined_recently: bool):
    """Extra member accounts (beyond the fixture's few), their MFA registration and Microsoft 365 use."""
    users, registration, active = [], [], []
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
        users.append({"id": uid, "displayName": name, "userPrincipalName": upn, "mail": upn, "userType": "Member",
                      "accountEnabled": True, "assignedLicenses": [{"skuId": sku}], "jobTitle": None,
                      "department": DEPARTMENTS[i % len(DEPARTMENTS)],
                      "createdDateTime": (now - timedelta(days=30 + 37 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "signInActivity": {"lastSignInDateTime": last.strftime("%Y-%m-%dT%H:%M:%SZ")}})
        registered = rng.random() < mfa_share and not inactive
        registration.append({"id": uid, "userPrincipalName": upn, "userDisplayName": name, "userType": "member",
                             "isAdmin": False, "isMfaRegistered": registered, "isMfaCapable": registered,
                             "methodsRegistered": ["microsoftAuthenticatorPush"] if registered else []})
        product = "MICROSOFT 365 BUSINESS PREMIUM" if sku == "sku-bp" else "MICROSOFT 365 BUSINESS BASIC"
        quiet = None if inactive else rng.choice([0, 1, 2, 4])
        active.append(usagedata.active_user(upn, [product], name=name, exchange=quiet, teams=quiet,
                                            onedrive=None if inactive else rng.choice([1, 3, 9, None])))
    return users, registration, active


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


def collect(db, tenant_id: int, t: dict, now: datetime, first: bool) -> None:
    from tests.seed import seed_tenant

    with Renamed(renamer(t)):
        # The same seed both times: the same people, two of whom join after the first collection.
        rng = random.Random(t["name"])  # noqa: S311 (demo data, not security)
        users, registration, active = staff(t, rng, now, t["mfa"][0 if first else 1], joined_recently=not first)
        everyone = fixture_users.USERS + users
        bp = sum(1 for u in everyone if {"skuId": "sku-bp"} in u.get("assignedLicenses", []))
        basic = sum(1 for u in everyone if {"skuId": "sku-basic"} in u.get("assignedLicenses", []))
        seed_tenant(db, tenant_id, dns=graphdata.fake_dns, users=everyone, skus=skus(t, bp, basic),
                    registration=graphdata.REGISTRATION_DETAILS + registration,
                    secure_scores=secure_scores(t["score"][0] - 4, t["score"][0], now) if first
                    else secure_scores(t["score"][0] - 4, t["score"][1], now, 105),
                    usage_kw={"active": usagedata.ACTIVE_USERS + active})


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


def populate(db, vault, *, msp: str = "Harbour IT", accent: str = "#0d7a5f",
             contact: str = "help@harbourit.co.uk · 0161 496 0000") -> list[int]:
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
        collect(db, tid, t, now, first=False)
        ids.append(tid)
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
    password = secrets.token_urlsafe(12)
    db.execute("INSERT INTO users (username, password_hash, role, all_tenants, created_at)"
               " VALUES (?, ?, 'admin', 1, ?)",
               (args.admin, generate_password_hash(password), utcnow()))
    print(f"Added {len(ids)} demo tenants to {cfg.db_path}")
    print(f"Sign in as {args.admin} / {password}")
    print(f"Start the portal: OFFICESENTRY_DATA_DIR={args.data_dir} python -m officesentry web")


if __name__ == "__main__":
    main()
