"""The demo data's reports add up: they become the public examples, screenshots and demo site, and an MSP
reading them checks the sums."""

import importlib.util
import re
from datetime import datetime
from pathlib import Path

from officesentry.reports import data_reports, summary
from officesentry.reports.monthly_review import build

from .conftest import login, make_user

UNITS = {"bytes": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


def _populate(db, vault) -> list[int]:
    path = Path(__file__).resolve().parents[1] / "scripts" / "demo_data.py"
    spec = importlib.util.spec_from_file_location("demo_data", path)
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    return demo.populate(db, vault)


def _bytes(text: str) -> float:
    number, unit = re.fullmatch(r"([\d.,]+) (bytes|KB|MB|GB|TB)", text.strip()).groups()
    return float(number.replace(",", "")) * UNITS[unit]


def _close(a: float, b: float, places: float) -> bool:
    """Equal within what showing them rounded can explain (``places``: the coarsest figure's last digit)."""
    return abs(a - b) <= places


def _identity_adds_up(db, tenant) -> None:
    """The identity reports show what a real tenant's do: nobody signed in before their account existed, every
    member has a password age, the recycle bin's people have names of their own, sign-ins from abroad come
    from a city there, and the client with Entra ID P2 has risky users to show and MFA on every admin."""
    inactive = data_reports.build(db, tenant, "inactive")
    members = next(s for s in inactive.sections if s.title == "Inactive members")
    cols = members.columns
    for row in members.rows:
        if row[cols.index("Last sign-in")] == "Never":  # a mailbox's account, which nobody signs in to
            continue
        last, created = (datetime.strptime(row[cols.index(c)], "%d %b %Y") for c in ("Last sign-in", "Created"))
        assert created < last, row
    methods = data_reports.build(db, tenant, "auth-methods")
    passwords = next(s for s in methods.sections if s.title == "Password age")
    changed = passwords.columns.index("Password last changed")
    assert all(r[changed] and r[changed] != "Unknown" for r in passwords.rows), passwords.rows
    deleted = data_reports.build(db, tenant, "deleted-items")
    gone = next(s for s in deleted.sections if s.title.startswith("Deleted users"))
    assert gone.rows and not any(re.search(r"Leaver|Recent|Old Guest|du[123]", str(r)) for r in gone.rows), gone.rows
    signins = data_reports.build(db, tenant, "sign-ins")
    abroad = next(s for s in signins.sections if s.title == "Successful sign-ins from unusual countries")
    country, cities = abroad.columns.index("Country"), abroad.columns.index("Cities")
    for row in abroad.rows:
        if row[country] in ("FR", "IE", "US"):
            assert "London" not in str(row[cities]), row
    risky = data_reports.build(db, tenant, "risky-users")
    if tenant["name"] == "Contoso Legal LLP":
        assert risky.unavailable is None and any(s.rows for s in risky.sections), tenant["name"]
    else:
        assert risky.unavailable, tenant["name"]
    # Contoso Legal is the best-run client: every admin has MFA, so the grades on the demo differ.
    critical = db.execute(
        "SELECT COUNT(*) FROM findings WHERE tenant_id = ? AND rule_key = 'admin_mfa' AND severity = 'critical'"
        " AND resolved_at IS NULL",
        (tenant["id"],),
    ).fetchone()[0]
    assert (critical == 0) == (tenant["name"] == "Contoso Legal LLP"), (tenant["name"], critical)


def test_demo_reports_add_up(app, db, vault):
    ids = _populate(db, vault)
    make_user(db, "auditor")
    client = app.test_client()
    login(client, "auditor")
    storage_checked = 0
    for tid in ids:
        tenant = db.execute("SELECT * FROM tenants WHERE id = ?", (tid,)).fetchone()

        # Monthly review: every new or returning finding counted is one listed under "What changed".
        report = build(db, tenant)
        assert report.new_count == len(report.new_findings)
        page = client.get(f"/tenants/{tid}/report").get_data(as_text=True)
        changes = page.split('id="changes"', 1)[1].split("</section>", 1)[0]
        findings = changes.split("In Microsoft 365", 1)[0]
        assert findings.count("chg-added") == min(report.new_count, 15), tenant["name"]
        assert findings.count("chg-resolved") == min(len(report.resolved), 15), tenant["name"]
        if report.new_count:
            assert f"{len(report.resolved)} fixed and {report.new_count} new or returning" in findings
            assert f"{report.new_count} new item" in " ".join(summary.monthly(report).paragraphs)

        # Guests: the fixtures' guest keeps its account type when its name is changed, and every guest has an
        # address, a name, the organisation it's from and an invitation date, as a real tenant's do.
        guests = data_reports.build(db, tenant, "guests")
        listed = next(s for s in guests.sections if s.title == "Guest accounts")
        assert listed.rows, tenant["name"]
        assert all(
            "@" in r[0] and "#EXT#" not in r[0] and r[1] and r[2] == r[0].rsplit("@", 1)[1] and r[3]
            for r in listed.rows
        ), listed.rows
        _identity_adds_up(db, tenant)

        # SharePoint storage: the parts make up what's used, and used plus free is the tenant's total.
        storage = data_reports.build(db, tenant, "sharepoint-storage")
        if storage.unavailable:
            continue
        storage_checked += 1
        tiles = {t.label: t for t in storage.tiles}
        used, total = (_bytes(x) for x in tiles["Storage used"].foot.removesuffix(" (estimated)").split(" of "))
        free = _bytes(tiles["Free space"].value)
        assert _close(used + free, total, 0.01 * UNITS["TB"]), (used, free, total)
        parts = next(s for s in storage.sections if s.title == "Where the space goes")
        assert _close(sum(_bytes(r[1]) for r in parts.rows), used, 0.02 * UNITS["TB"]), parts.rows
        shares = [int(r[2].rstrip("%")) for r in parts.rows if r[2][:1].isdigit()]
        assert 98 <= sum(shares) <= 102, parts.rows
        sites = next(s for s in storage.sections if s.title == "Sites by storage")
        for row in sites.rows:  # a site's storage is at least these (plus Preservation Hold, lists and pages)
            used_site, current, versions, recycle = (_bytes(str(x)) for x in row[1:5])
            assert current + versions + recycle <= used_site + 0.3 * UNITS["GB"], row
    assert storage_checked
