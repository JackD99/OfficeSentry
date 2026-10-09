"""The demo data's reports add up: they become the public examples, screenshots and demo site, and an MSP
reading them checks the sums."""

import importlib.util
import re
from pathlib import Path

from officesentry.reports import data_reports, summary
from officesentry.reports.monthly_review import build

from .conftest import login, make_user

UNITS = {"bytes": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}


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
        findings = changes.split("In the tenant", 1)[0]
        assert findings.count("chg-added") == min(report.new_count, 15), tenant["name"]
        assert findings.count("chg-resolved") == min(len(report.resolved), 15), tenant["name"]
        if report.new_count:
            assert f"{len(report.resolved)} fixed and {report.new_count} new or returning" in findings
            assert f"{report.new_count} new item" in " ".join(summary.monthly(report).paragraphs)

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
