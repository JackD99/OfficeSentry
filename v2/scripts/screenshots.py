"""Screenshots and example reports for the README, from fictional demo data.

Seeds a throwaway database with a made-up MSP and made-up client tenants
(``scripts/demo_data.py``, see ``seed()``), runs the portal on a free local port, and
uses Playwright's Chromium to save:

* ``docs/screenshots/*.png``: pages at 1440 px wide (light), two at phone
  width and one in dark mode, plus the first pages of report PDFs;
* ``examples/*``: real exported PDFs and an XLSX, with a README listing them
  (``--no-examples`` skips them).

Re-run it whenever the UI or the reports change, then look at the images
before committing them::

    cd v2
    pip install -e '.[dev]' playwright pillow   # never `playwright install` here: it uses the browsers
                                                 # in $PLAYWRIGHT_BROWSERS_PATH (e.g. /opt/pw-browsers)
    python scripts/screenshots.py                # everything
    python scripts/screenshots.py --only sharepoint-storage,review   # some shots (names below)
    python scripts/screenshots.py --no-examples  # screenshots only
    python scripts/screenshots.py --serve        # seed and keep the portal running to look around

Needs WeasyPrint (PDFs) and ``pdftoppm`` (poppler-utils) for the PDF page
images; without them those are skipped with a message. Outputs go to the
repository root (``--out`` and ``--examples-dir`` change that). The data is
fictional: companies, people and ``.example`` domains are made up.

To use other demo data, change ``seed()``: it only has to fill a migrated
database and return the tenant ids, the first one being the showcase tenant.
"""

from __future__ import annotations

import argparse
import glob
import os
import secrets
import shutil
import subprocess  # noqa: S404 (runs pdftoppm on files this script wrote)
import sys
import tempfile
import textwrap
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

V2 = Path(__file__).resolve().parents[1]
ROOT = V2.parent
sys.path.insert(0, str(V2))
sys.path.insert(0, str(V2 / "scripts"))

from werkzeug.security import generate_password_hash  # noqa: E402

from officesentry import store  # noqa: E402
from officesentry.db import utcnow  # noqa: E402

ADMIN = "demo"
CLIENT = "client"


SHOWCASE = "Fabrikam Dental Group"  # the tenant most pages show: SharePoint nearly full, plenty to fix
ONBOARDING = "Kingsley Optometry"  # added without consent: "{o}" in a shot's path


def seed(db, vault) -> list[int]:
    """Fill a migrated database with demo data and return the tenant ids, the showcase tenant first."""
    import demo_data

    ids = demo_data.populate(db, vault)
    names = {db.execute("SELECT name FROM tenants WHERE id = ?", (i,)).fetchone()[0]: i for i in ids}
    first = names.get(SHOWCASE, ids[0])
    ids = [first] + [i for i in ids if i != first]
    add_email(db, first)
    add_project_statuses(db, ids)
    demo_data.team(db, ids)  # the MSP's people, one per role, for the Users page
    # A client part-way through onboarding (consent not given yet), for the Client tenants page and checklist.
    connection = db.execute("SELECT id FROM app_connections ORDER BY id LIMIT 1").fetchone()[0]
    onboarding = store.create_tenant(db, ONBOARDING, "kingsleyoptometry.onmicrosoft.com", connection)
    sent = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute(
        "INSERT INTO onboarding (tenant_id, contact_name, contact_email, sent_at, sent_by) VALUES (?, ?, ?, ?, ?)",
        (onboarding, "Priya Kingsley", "priya@kingsleyoptometry.example", sent, ADMIN),
    )
    ids.append(onboarding)
    return ids


def add_email(db, tenant_id: int) -> None:
    """SMTP settings, contacts, a monthly report schedule that has sent, an alert email and a delivery history.

    The SMTP server is made up; nothing is sent, since no worker runs.
    """
    import json
    import re
    from datetime import UTC, datetime, timedelta

    from officesentry import delivery

    def iso(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    contact = (db.execute("SELECT value FROM settings WHERE key = 'brand_contact'").fetchone() or [""])[0]
    msp_domain = (re.search(r"@([\w.-]+)", contact) or [None, "msp.example"])[1]
    domain = db.execute(
        "SELECT object_id FROM objects o JOIN runs r ON r.id = o.run_id WHERE r.tenant_id = ?"
        " AND o.kind = 'accepted_domain' ORDER BY r.id DESC LIMIT 1",
        (tenant_id,),
    ).fetchone()
    domain = domain[0] if domain else "client.example"
    for key, value in (
        ("mail_transport", "smtp"),
        ("smtp_host", f"smtp.{msp_domain}"),
        ("smtp_port", "587"),
        ("smtp_security", "starttls"),
        ("smtp_from", f"reports@{msp_domain}"),
    ):
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    people = [
        delivery.add_contact(db, tenant_id, name, f"{local}@{domain}")
        for name, local in (("Practice manager", "practice.manager"), ("Finance director", "finance"))
    ]
    team = delivery.add_contact(db, None, "Service desk", f"servicedesk@{msp_domain}")
    now = datetime.now(UTC)
    reports = ["review", "evidence", "data:sharepoint-storage", "data:licence-rightsizing"]
    sid = db.execute(
        "INSERT INTO email_schedules (kind, tenant_id, reports, include_xlsx, message, frequency, day_of_month, hour,"
        " timezone, last_period, last_status, last_sent_at, created_by, created_at)"
        " VALUES ('report', ?, ?, 1, ?, 'monthly', 1, 8, 'Europe/London', ?, 'sent', ?, ?, ?)",
        (
            tenant_id,
            json.dumps(reports),
            "Your monthly review is attached. Give us a call if anything needs a hand.",
            now.strftime("%Y-%m"),
            iso(now - timedelta(days=3)),
            ADMIN,
            iso(now - timedelta(days=70)),
        ),
    ).lastrowid
    delivery.set_recipients(db, "schedule", sid, people + [team])
    alert = db.execute(
        "INSERT INTO email_alerts (tenant_id, min_severity, created_by, created_at) VALUES (?, 'high', ?, ?)",
        (tenant_id, ADMIN, iso(now - timedelta(days=70))),
    ).lastrowid
    delivery.set_recipients(db, "alert", alert, [team])
    to = ", ".join(f"{local}@{domain}" for local in ("practice.manager", "finance"))
    files = json.dumps(
        [
            "monthly-review.pdf",
            "monthly-review.xlsx",
            "insurance-evidence.pdf",
            "sharepoint-storage.xlsx",
            "licence-right-sizing.xlsx",
        ]
    )
    name = db.execute("SELECT name FROM tenants WHERE id = ?", (tenant_id,)).fetchone()[0]
    for kind, days, subject, recipients, bcc, attachments, schedule, alert_id in (
        (
            "report",
            3,
            f"{name}: Monthly Security & Licensing Review, September 2026",
            to,
            f"servicedesk@{msp_domain}",
            files,
            sid,
            None,
        ),
        ("alert", 9, f"{name}: 2 new high findings", f"servicedesk@{msp_domain}", "", "[]", None, alert),
        (
            "report",
            33,
            f"{name}: Monthly Security & Licensing Review, August 2026",
            to,
            f"servicedesk@{msp_domain}",
            files,
            sid,
            None,
        ),
    ):
        db.execute(
            "INSERT INTO email_deliveries (kind, schedule_id, alert_id, tenant_id, period, subject, recipients,"
            " bcc, attachments, transport, trigger, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'smtp', 'schedule', 'sent', ?)",
            (
                kind,
                schedule,
                alert_id,
                tenant_id,
                None,
                subject,
                recipients,
                bcc,
                attachments,
                iso(now - timedelta(days=days, hours=-8)),
            ),
        )


def add_project_statuses(db, ids: list[int]) -> None:
    """Where the account managers have got to with a few recommended projects."""
    from officesentry import projects

    for tenant_id, (key, status, note) in zip(
        ids,
        [
            ("identity", "proposed", "Quote sent to the practice manager"),
            ("email_security", "agreed", "Booked for the first week of next month"),
            ("device_management", "proposed", ""),
        ],
        strict=False,
    ):
        projects.set_status(db, tenant_id, key, status, note, ADMIN)


# -- What to capture ------------------------------------------------------------------------


@dataclass
class Shot:
    name: str
    path: str  # "{t}" is the showcase tenant's id, "{o}" the one being onboarded, "{u}" a first-line technician
    height: int = 900  # viewport height: the picture is what fits in it
    width: int = 1440
    scroll_to: str | None = None  # CSS selector (or text=...) to bring to the top first
    dark: bool = False
    phone: bool = False
    user: str = ADMIN
    hide: list[str] = field(default_factory=list)  # selectors hidden before the capture


SHOTS = [
    Shot("dashboard", "/", height=640),
    Shot("tenant-overview", "/tenants/{t}", height=1000),
    Shot("review", "/tenants/{t}/report", height=1100),
    Shot("review-attention", "/tenants/{t}/report", height=1000, scroll_to="#month"),
    Shot("sharepoint-storage", "/tenants/{t}/data/sharepoint-storage", height=1100),
    Shot("site-access", "/tenants/{t}/data/site-access", height=1100),
    Shot("site-permissions", "/tenants/{t}/data/site-permissions", height=1100),
    Shot(
        "sharepoint-storage-files",
        "/tenants/{t}/data/sharepoint-storage",
        height=950,
        scroll_to="h2:has-text('Largest files')",
    ),
    Shot("mfa", "/tenants/{t}/data/mfa", height=950),
    Shot("mail-forwarding", "/tenants/{t}/data/mailboxes", height=900),
    Shot("mailbox-permissions", "/tenants/{t}/data/mailbox-permissions", height=950),
    Shot("licence-rightsizing", "/tenants/{t}/data/licence-rightsizing", height=1000),
    Shot("conditional-access", "/tenants/{t}/data/conditional-access", height=950),
    Shot("devices", "/tenants/{t}/data/devices", height=950),
    Shot("sign-ins", "/tenants/{t}/data/sign-ins", height=950),
    Shot("auth-methods", "/tenants/{t}/data/auth-methods", height=950),
    Shot("subscriptions", "/tenants/{t}/data/subscriptions", height=950),
    Shot("reports-library", "/reports", height=1000),
    Shot("person", "/tenants/{t}/users/u1", height=1100),
    Shot("all-tenants", "/across/review", height=760),
    Shot("what-changed", "/changes", height=640),
    Shot("upcoming", "/upcoming", height=900),
    Shot("activity-history", "/tenants/{t}/activity", height=1000),
    Shot("tenant-groups", "/admin/groups", height=640),
    Shot("users", "/admin/users", height=760),
    Shot("user-role", "/admin/users/{u}", height=1000),
    Shot("roles", "/admin/roles", height=760),
    Shot("role-edit", "/admin/roles/4", height=1000),  # First line, a built-in role (ids are seeded)
    Shot("system-status", "/admin/status", height=900),
    Shot("evidence", "/tenants/{t}/evidence", height=1000),
    Shot("emailed-reports", "/tenants/{t}/schedules", height=900),
    Shot("email-schedule-form", "/tenants/{t}/schedules/new", height=1000),
    Shot("projects", "/projects", height=950),
    Shot("tenant-projects", "/tenants/{t}/projects", height=1100),
    Shot("client-tenants", "/admin/tenants", height=700),
    Shot("manage-tenant", "/tenants/{t}/manage", height=1000),
    Shot("onboarding", "/tenants/{o}", height=900),
    Shot("client-view", "/", height=420, user=CLIENT),
    Shot("phone-review", "/tenants/{t}/report", width=390, height=844, phone=True),
    Shot("phone-overview", "/tenants/{t}", width=390, height=844, phone=True),
    Shot("dark-sharepoint-storage", "/tenants/{t}/data/sharepoint-storage", height=900, dark=True),
]

# PDFs whose first page becomes a picture: name -> path.
PDF_SHOTS = {
    "pdf-review": "/tenants/{t}/report.pdf",
    "pdf-evidence": "/tenants/{t}/evidence.pdf",
    "pdf-sharepoint-storage": "/tenants/{t}/data/sharepoint-storage.pdf",
}

# Example downloads for examples/: file name -> (path, one line for examples/README.md).
EXAMPLES = {
    "monthly-security-licensing-review.pdf": (
        "/tenants/{t}/report.pdf",
        "The Monthly Security & Licensing Review for one client, as sent to them.",
    ),
    "monthly-review-summary.pdf": (
        "/tenants/{t}/report.pdf?appendix=0",
        "The monthly review's two summary pages on their own, without the appendix.",
    ),
    "quarterly-review.pdf": (
        "/tenants/{t}/designs/1.pdf",
        "The Quarterly review design from the report designer: three months of score and findings, what was "
        "fixed, projects, licence spend and what's coming up.",
    ),
    "insurance-evidence-pack.pdf": (
        "/tenants/{t}/evidence.pdf",
        "The controls cyber-insurance questionnaires ask about, answered from the data.",
    ),
    "sharepoint-storage.pdf": (
        "/tenants/{t}/data/sharepoint-storage.pdf",
        "A client nearly out of SharePoint space: where it went, when it fills up, what to clean up.",
    ),
    "mfa-coverage.pdf": ("/tenants/{t}/data/mfa.pdf", "Who has MFA registered and who is enforced to use it."),
    "mailbox-permissions.pdf": (
        "/tenants/{t}/data/mailbox-permissions.pdf",
        "Full access, send as and send on behalf, with leavers and outsiders flagged.",
    ),
    "mail-forwarding.pdf": (
        "/tenants/{t}/data/mailboxes.pdf",
        "Every mailbox and inbox rule that forwards mail, and the policies.",
    ),
    "licence-right-sizing.pdf": (
        "/tenants/{t}/data/licence-rightsizing.pdf",
        "People who could move to a cheaper licence, with the saving.",
    ),
    "conditional-access-coverage.pdf": (
        "/tenants/{t}/data/conditional-access.pdf",
        "Every Conditional Access policy in plain words, and who it covers.",
    ),
    "sign-in-activity.pdf": (
        "/tenants/{t}/data/sign-ins.pdf",
        "Failed and successful sign-ins: password guessing, unusual countries.",
    ),
    "passwords-and-sign-in-methods.pdf": (
        "/tenants/{t}/data/auth-methods.pdf",
        "How people sign in, how strong that is, and admins without passkeys.",
    ),
    "licences-and-subscriptions.pdf": (
        "/tenants/{t}/data/subscriptions.pdf",
        "Every subscription, what is assigned and what is paid for but unused.",
    ),
    "account-page.pdf": ("/tenants/{t}/users/u1.pdf", "Everything about one person's account on one page."),
    "what-changed-all-clients.pdf": ("/changes.pdf", "Month-over-month changes across every client."),
    "all-clients-monthly-review.pdf": (
        "/across/review.pdf",
        "One row per client: the monthly review's key figures side by side.",
    ),
    "recommended-projects.pdf": (
        "/tenants/{t}/projects.pdf",
        "The projects a client's Microsoft 365 calls for, as a proposal to send them.",
    ),
    "monthly-security-licensing-review.xlsx": (
        "/tenants/{t}/report.xlsx",
        "The monthly review as a spreadsheet: every finding and figure.",
    ),
}


# -- Demo site ------------------------------------------------------------------------------


def build_site(data_dir: Path):
    """Seed a new, empty data directory; returns (config, tenant ids, passwords by username, names)."""
    from datetime import UTC, datetime, timedelta

    from officesentry import backups, system_health, worker
    from officesentry.config import load_config
    from officesentry.crypto import Vault
    from officesentry.db import connect, migrate
    from officesentry.reports import branding

    if data_dir.exists() and any(data_dir.iterdir()):
        sys.exit(f"{data_dir} isn't empty; use a new data directory.")
    cfg = load_config(data_dir=data_dir, secure_cookies=False, require_mfa="off")
    db = connect(cfg.db_path)
    migrate(db)
    ids = seed(db, Vault(cfg.encryption_keys))
    passwords = {ADMIN: secrets.token_urlsafe(12), CLIENT: secrets.token_urlsafe(12)}
    for username, role, all_tenants in ((ADMIN, "admin", 1), (CLIENT, "client", 0)):
        uid = db.execute(
            "INSERT INTO users (username, password_hash, role, all_tenants, created_at) VALUES (?, ?, ?, ?, ?)",
            (username, generate_password_hash(passwords[username]), role, all_tenants, utcnow()),
        ).lastrowid
        if not all_tenants:
            db.execute("INSERT INTO user_tenants (user_id, tenant_id) VALUES (?, ?)", (uid, ids[0]))
    worker.beat(db)
    # Last night's backups and a roomy disk, so System status shows a looked-after server, not this machine's disk.
    now = datetime.now(UTC)
    night = now.replace(hour=3, minute=40, second=0, microsecond=0)
    night -= timedelta(days=1) if night > now else timedelta()
    for job, detail in (
        ("local", "officesentry-backup.db.gz, 48.2 MB"),
        ("offsite", "Copied off-site"),
        ("restore_check", "The newest off-site copy opened and checked out"),
    ):
        backups.record_status(data_dir, job, True, detail=detail, now=night)
    system_health.disk_usage = lambda path: (38.0, 62 * 2**30, 100 * 2**30)
    names = {
        "msp": branding.load(db).display_name,
        "tenants": [db.execute("SELECT name FROM tenants WHERE id = ?", (i,)).fetchone()[0] for i in ids],
    }
    db.close()
    return cfg, ids, passwords, names


class Server:
    """The portal on a free port, in a thread, with a fresh worker heartbeat so no warning banner shows."""

    def __init__(self, cfg):
        import logging

        from werkzeug.serving import make_server

        from officesentry.web import create_app

        self.cfg = cfg
        logging.getLogger("werkzeug").setLevel(logging.ERROR)  # no line per request
        self.http = make_server("127.0.0.1", 0, create_app(cfg), threaded=True)
        self.url = f"http://127.0.0.1:{self.http.server_port}"
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.http.shutdown()

    def beat(self):
        from officesentry import worker
        from officesentry.db import connect

        db = connect(self.cfg.db_path)
        try:
            worker.beat(db)
        finally:
            db.close()

    def user_id(self, username: str) -> int:
        from officesentry.db import connect

        db = connect(self.cfg.db_path)
        try:
            return db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
        finally:
            db.close()


# -- Browser -----------------------------------------------------------------------------------


def chromium_path() -> str | None:
    """Playwright's own Chromium if it is installed, else any Chromium under $PLAYWRIGHT_BROWSERS_PATH."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        if Path(p.chromium.executable_path).exists():
            return None
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    found = sorted(glob.glob(f"{root}/chromium-*/chrome-linux*/chrome"), reverse=True)
    if not found:
        sys.exit(f"No Chromium found for Playwright under {root}; set PLAYWRIGHT_BROWSERS_PATH.")
    return found[0]


def sign_in(context, url: str, username: str, password: str) -> None:
    page = context.new_page()
    page.goto(f"{url}/login")
    page.fill("input[name=username]", username)
    page.fill("input[name=password]", password)
    with page.expect_navigation():
        page.press("input[name=password]", "Enter")
    page.wait_for_load_state("networkidle")
    if "/login" in page.url:
        sys.exit(f"Couldn't sign in as {username}")
    page.close()


def optimise(png: bytes, path: Path, max_width: int | None = None) -> None:
    """Save a PNG with a 256-colour palette: UI screenshots stay sharp at a fraction of the size."""
    from PIL import Image

    img = Image.open(BytesIO(png)).convert("RGB")
    if max_width and img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)), Image.Resampling.LANCZOS)
    img.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(path, optimize=True)


def capture(server: Server, passwords: dict, ids: list[int], shots: list[Shot], out: Path) -> list[str]:
    from playwright.sync_api import sync_playwright

    saved = []
    executable = chromium_path()
    technician = server.user_id("jordan.desk")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=executable)
        contexts = {}
        try:
            for shot in shots:
                key = (shot.user, shot.dark, shot.phone)
                if key not in contexts:
                    options = {
                        "viewport": {"width": shot.width, "height": shot.height},
                        "color_scheme": "dark" if shot.dark else "light",
                        "device_scale_factor": 1,
                    }
                    if shot.phone:
                        options.update(device_scale_factor=2, is_mobile=True, has_touch=True)
                    contexts[key] = browser.new_context(**options)
                    sign_in(contexts[key], server.url, shot.user, passwords[shot.user])
                server.beat()
                page = contexts[key].new_page()
                page.set_viewport_size({"width": shot.width, "height": shot.height})
                response = page.goto(server.url + shot.path.format(t=ids[0], o=ids[-1], u=technician))
                page.wait_for_load_state("networkidle")
                if response is None or response.status != 200:
                    print(f"  {shot.name}: {shot.path} returned {response.status if response else 'nothing'}")
                    page.close()
                    continue
                for selector in shot.hide:
                    page.add_style_tag(content=f"{selector} {{ display: none !important; }}")
                if shot.scroll_to:
                    target = page.locator(shot.scroll_to).first
                    if target.count():
                        target.evaluate(  # just under the sticky report toolbar, when the page has one
                            "el => { const bar = document.querySelector('nav.toolbar');"
                            " const top = el.getBoundingClientRect().top + window.scrollY;"
                            " window.scrollTo(0, top - (bar ? bar.offsetHeight : 0) - 16); }"
                        )
                    else:
                        print(f"  {shot.name}: {shot.scroll_to!r} not found; capturing the top")
                page.wait_for_timeout(150)
                optimise(page.screenshot(), out / f"{shot.name}.png", max_width=shot.width if shot.phone else None)
                saved.append(shot.name)
                page.close()
        finally:
            browser.close()
    return saved


def download(server: Server, passwords: dict, paths: dict[str, str]) -> dict[str, bytes]:
    """Fetch report files as the admin: name -> body."""
    import requests

    http = requests.Session()
    login = http.get(f"{server.url}/login")
    token = _csrf(login.text)
    resp = http.post(f"{server.url}/login", data={"username": ADMIN, "password": passwords[ADMIN], "csrf_token": token})
    if "/login" in resp.url:
        sys.exit("Couldn't sign in to download reports")
    files = {}
    for name, path in paths.items():
        server.beat()
        r = http.get(server.url + path, timeout=300)
        kind = r.headers.get("Content-Type", "")
        if r.status_code != 200 or not ("pdf" in kind or "spreadsheet" in kind):
            print(f"  {name}: {path} returned {r.status_code} {kind} (is WeasyPrint installed?)")
            continue
        files[name] = r.content
    return files


def _csrf(html: str) -> str:
    import re

    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html) or re.search(
        r'value="([^"]+)"[^>]*name="csrf_token"', html
    )
    return m.group(1) if m else ""


def pdf_first_page(pdf: bytes, png_path: Path, dpi: int = 90) -> bool:
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        print("  pdftoppm not found (install poppler-utils); skipping PDF page images")
        return False
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "report.pdf"
        src.write_bytes(pdf)
        subprocess.run(  # noqa: S603
            [
                pdftoppm,
                "-png",
                "-r",
                str(dpi),
                "-f",
                "1",
                "-l",
                "1",
                "-singlefile",
                str(src),
                str(Path(tmp) / "page"),
            ],
            check=True,
        )
        optimise((Path(tmp) / "page.png").read_bytes(), png_path)
    return True


def write_examples(files: dict[str, bytes], out: Path, msp: str, tenants: list[str]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for old in [*out.glob("*.pdf"), *out.glob("*.xlsx")]:
        if old.name not in files:  # an example that is no longer made
            old.unlink()
    for name, body in files.items():
        (out / name).write_bytes(body)
    about = (
        f"Real exports from Office Sentry, made from **fictional demo data**. {msp} is a made-up MSP and its "
        f"clients ({', '.join(tenants)}) are made-up companies; every person, address and `.example` domain "
        f"is invented. Nothing here comes from a real Microsoft 365 tenant. The single-client reports are for "
        f"{tenants[0]}."
    )
    lines = [
        "# Example reports",
        "",
        textwrap.fill(about, 100),
        "",
        "Regenerate them from `v2/` with `python scripts/screenshots.py` after report changes.",
        "",
        "| File | What it is |",
        "|---|---|",
    ]
    lines += [f"| [{name}]({name}) | {EXAMPLES[name][1]} |" for name in EXAMPLES if name in files]
    (out / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# -- Main -----------------------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "screenshots")
    parser.add_argument("--examples-dir", type=Path, default=ROOT / "examples")
    parser.add_argument("--only", help="comma-separated shot names, e.g. review,mfa (PDF pages: pdf-review)")
    parser.add_argument("--no-examples", action="store_true", help="don't write examples/")
    parser.add_argument("--data-dir", type=Path, help="keep the demo database here (default: a temporary folder)")
    parser.add_argument("--serve", action="store_true", help="seed, print the sign-in and keep the portal running")
    args = parser.parse_args()

    only = set(args.only.split(",")) if args.only else None
    with tempfile.TemporaryDirectory(prefix="officesentry-shots-") as tmp:
        data_dir = args.data_dir or Path(tmp) / "data"
        print(f"Seeding demo data in {data_dir}")
        cfg, ids, passwords, names = build_site(data_dir)
        with Server(cfg) as server:
            if args.serve:
                print(
                    f"Portal: {server.url}  sign in as {ADMIN} / {passwords[ADMIN]}"
                    f"  (client: {CLIENT} / {passwords[CLIENT]}). Ctrl+C to stop."
                )
                try:
                    threading.Event().wait()
                except KeyboardInterrupt:
                    return
            args.out.mkdir(parents=True, exist_ok=True)
            shots = [s for s in SHOTS if only is None or s.name in only]
            print(f"Capturing {len(shots)} pages to {args.out}")
            saved = capture(server, passwords, ids, shots, args.out)
            pdfs = {n: p.format(t=ids[0]) for n, p in PDF_SHOTS.items() if only is None or n in only}
            examples = {} if args.no_examples or only else {n: p.format(t=ids[0]) for n, (p, _) in EXAMPLES.items()}
            files = download(server, passwords, {**pdfs, **examples})
            for name in pdfs:
                if name in files and pdf_first_page(files[name], args.out / f"{name}.png"):
                    saved.append(name)
            if examples:
                write_examples(
                    {n: files[n] for n in examples if n in files}, args.examples_dir, names["msp"], names["tenants"]
                )
                print(f"Wrote {len([n for n in examples if n in files])} example files to {args.examples_dir}")
    total = sum((args.out / f"{n}.png").stat().st_size for n in saved)
    print(f"Saved {len(saved)} images ({total / 1024 / 1024:.1f} MB). Look at every one before committing.")


if __name__ == "__main__":
    main()
