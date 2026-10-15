"""The support package (support.py, Settings → Support, ``support-package``) and the log context (logs.py).

The package must carry what a maintainer needs and nothing that identifies a client, a person or a secret:
the tests plant each kind of secret and name in the places a real install would have them, build the
package, and look for them in every byte of the zip.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from officesentry import logs, store, support, tasks, worker
from officesentry.__main__ import main
from officesentry.db import audit, utcnow
from officesentry.web import create_app
from tests.conftest import login, make_user

NOW = datetime(2026, 10, 10, 22, 0, tzinfo=UTC)
TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJhdWQiOiJodHRwczovL2dyYXBoIn0.c2lnbmF0dXJlLXBhcnQtb2YtdGhlLXRva2Vu"
GUID = "72f988bf-86f1-41af-91ab-2d7cd011db47"


def _bytes(pkg: support.Package) -> bytes:
    return b"\n".join(pkg.files.values())


# -- Redaction ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "gone", "kept"),
    [
        (f"Bearer {TOKEN} was refused", [TOKEN], ["was refused"]),
        (f"Authorization: Bearer {TOKEN}", [TOKEN], ["Authorization: [removed]"]),
        ("stored gAAAAABnZ2dkZ2dkZ2dkZ2dkZ2dkZ2dkZ2dkZ2dkZ2dkZ2dk= for later", ["gAAAAAB"], ["stored", "for later"]),
        (
            "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----",
            ["MIIEvQ"],
            ["[certificate or key removed]"],
        ),
        ("password=hunter2&next=/home", ["hunter2"], ["password=[removed]", "next=/home"]),
        ("smtp_password: 's3cret!'", ["s3cret"], ["smtp_password: '[removed]"]),
        ("Set-Cookie: __Host-officesentry=abc.def; Path=/", ["abc.def"], ["Set-Cookie: [removed]"]),
        ("mail to adele.vance@contoso.com bounced", ["adele.vance", "contoso.com"], ["mail to [email] bounced"]),
        (
            "site contoso.sharepoint.com and contoso.onmicrosoft.com",
            ["contoso."],
            ["[tenant domain] and [tenant domain]"],
        ),
        ("from 203.0.113.9 and 2001:db8:85a3:0:0:8a2e:370:7334", ["203.0.113.9", "2001:db8"], ["from [ip] and [ip]"]),
        ("at 2026-10-10T22:33:47Z the clock read 22:33:47", [], ["2026-10-10T22:33:47Z", "22:33:47"]),
        ('File "C:\\Users\\jack\\officesentry\\app.py"', ["jack"], ["C:\\Users\\[user]\\officesentry"]),
        (
            "thumbprint 0123456789ABCDEF0123456789ABCDEF01234567 expired",
            ["0123456789ABCDEF"],
            ["thumbprint [long value removed] expired"],
        ),
        (f"tenant {GUID} refused", [GUID], ["tenant [guid] refused"]),
        ("?code=AQABAAI&state=xyz&next=1", ["AQABAAI", "xyz"], ["code=[removed]", "state=[removed]", "next=1"]),
        ("health.token_hash and the token_hash column", [], ["health.token_hash and the token_hash column"]),
        ('{"client_secret": "Abc8Q~xyz.value_123"}', ["Abc8Q"], ['"client_secret": "[removed]"']),
        ('password: "correct horse battery" given', ["horse", "battery"], ['password: "[removed]" given']),
        ("{'Authorization': 'Basic dXNlcjpwYXNz'}", ["dXNlcjpwYXNz"], ["{'Authorization': '[removed]'}"]),
        ("Bearer abc123shorttoken refused", ["abc123"], ["Bearer [removed] refused"]),
        ("Bearer authentication failed", [], ["Bearer authentication failed"]),
        ("cookie __Host-officesentry=eyJzaWQiOiJ4In0.ZmFrZQ.c2ln", ["eyJzaWQi"], ["[token removed]"]),
        ("allowed 2001:db8::/32, fe80::1 and ::1", ["db8", "fe80"], ["allowed [ip], [ip] and [ip]"]),
        ("lasted 0:00:05 and std::vector", [], ["0:00:05", "std::vector"]),
    ],
)
def test_redactor_removes_secrets_and_addresses(text, gone, kept):
    out = support.Redactor()(text)
    for g in gone:
        assert g not in out, out
    for k in kept:
        assert k in out, out


def test_redactor_replaces_client_and_user_names_unless_asked_to_keep_them(db, two_tenants):
    alpha, bravo = two_tenants
    make_user(db, "jack")
    names = support.names_to_hide(db)
    text = "Alpha Ltd (alpha.onmicrosoft.com) failed; jack retried; Bravo Inc was fine; ALPHA LTD again"
    out = support.Redactor(names)(text)
    want = (
        f"[client {alpha}] ([tenant domain]) failed; [user] retried; [client {bravo}] was fine; [client {alpha}] again"
    )
    assert out == want
    kept = support.Redactor(names, keep_identifiers=True)(text)
    assert "Alpha Ltd" in kept and "jack" in kept and f"{GUID}" == support.Redactor(names, keep_identifiers=True)(GUID)


# -- The package ---------------------------------------------------------------------------------------


def _world(db, cfg, two_tenants):
    """Secrets and names everywhere a real install has them: a failed run with a traceback naming a person
    and a client, a failed task, activity, and the app connection's certificate and key."""
    alpha, bravo = two_tenants
    make_user(db, "jack")
    run_id = store.enqueue_run(db, alpha, "users")
    store.claim_next_run(db)
    store.log_event(
        db,
        run_id,
        "error",
        'Traceback (most recent call last):\n  File "/home/jack/x.py"\nGraphError: 403 for '
        f"adele.vance@alpha.onmicrosoft.com in Alpha Ltd ({GUID}) with Bearer {TOKEN}",
    )
    store.finish_run(db, run_id, "failed", f"Sign-in failed: Alpha Ltd refused the app (tenant {GUID})")
    ok = store.enqueue_run(db, bravo, "mfa")
    store.claim_next_run(db)
    store.finish_run(db, ok, "succeeded")
    task_id = db.execute(
        "INSERT INTO tasks (kind, tenant_id, status, attempts, error, created_at, finished_at)"
        " VALUES ('approve', ?, 'failed', 1, ?, ?, ?)",
        (bravo, "Nobody to email at Bravo Inc: finance@bravo.onmicrosoft.com bounced", utcnow(), utcnow()),
    ).lastrowid
    audit(db, "jack", "tenant_created", tenant_id=alpha, detail="Alpha Ltd by jack from 203.0.113.9")
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('smtp_password_enc', 'gAAAAABsecretsecretsecretsecretsecret=')"
    )
    db.execute("INSERT INTO settings (key, value) VALUES ('retention.keep_daily', '45')")
    db.execute("INSERT INTO settings (key, value) VALUES ('brand_name', 'Northwind IT')")
    conn_row = db.execute("SELECT cert_pem, key_enc, thumbprint FROM app_connections").fetchone()
    return {
        "run": run_id,
        "task": task_id,
        "cert": conn_row["cert_pem"],
        "key": conn_row["key_enc"],
        "thumb": conn_row["thumbprint"],
    }


def test_package_carries_the_facts_and_none_of_the_secrets(cfg, db, two_tenants, monkeypatch):
    monkeypatch.setenv("OFFICESENTRY_SMTP_PASSWORD", "topsecret")
    monkeypatch.setenv("OFFICESENTRY_TIMEZONE", "Europe/London")
    world = _world(db, cfg, two_tenants)
    alpha, bravo = two_tenants
    logs.setup(cfg.data_dir, "web")
    logging.getLogger("officesentry.test").error("Alpha Ltd page failed for jack <adele@alpha.onmicrosoft.com>")

    pkg = support.build(db, cfg, now=NOW)
    assert pkg.filename == "officesentry-support-20261010-220000.zip" and not pkg.problems
    assert set(pkg.files) >= {
        "README.txt",
        "summary.json",
        "environment.json",
        "health.json",
        "database.json",
        "clients.json",
        "failures.json",
        "worker.json",
        "connections.json",
        "activity.json",
        "packages.txt",
        "logs/web.log",
        "logs/worker.log",
    }
    everything = _bytes(pkg).decode()

    # The facts a maintainer needs.
    summary = json.loads(pkg.files["summary.json"])
    assert summary["version"] and summary["schema_version"] == summary["schema_expected"]
    assert summary["errors_last_24h"] == {"web": 1, "worker": 0}
    env = json.loads(pkg.files["environment.json"])
    assert (
        env["variables"]["OFFICESENTRY_SMTP_PASSWORD"] == "[set]"
        and env["variables"]["OFFICESENTRY_TIMEZONE"] == "Europe/London"
    )
    assert env["effective"]["encryption_keys"] == 1 and "secret_key" not in env["effective"]
    database = json.loads(pkg.files["database.json"])
    assert database["tables"]["tenants"] == 2 and database["settings"]["retention.keep_daily"] == "45"
    assert database["settings"]["smtp_password_enc"].startswith("[set, ") and "brand_name" in database["settings"]
    clients = {c["id"]: c for c in json.loads(pkg.files["clients.json"])}
    assert (
        clients[alpha]["latest_runs"]["users"]["status"] == "failed"
        and clients[bravo]["latest_runs"]["mfa"]["status"] == "succeeded"
    )
    assert "name" not in clients[alpha] and clients[alpha]["consented"] is True
    failures = json.loads(pkg.files["failures.json"])
    assert failures["runs"][0]["run"] == world["run"] and failures["runs"][0]["tenant"] == alpha
    assert failures["runs"][0]["error"] == f"Sign-in failed: [client {alpha}] refused the app (tenant [guid])"
    assert "Traceback" in failures["runs"][0]["events"][0]["message"] and failures["failed_runs_by_collector"] == {
        "users": 1
    }
    assert (
        failures["tasks"][0]["task"] == world["task"]
        and failures["tasks"][0]["error"] == f"Nobody to email at [client {bravo}]: [email] bounced"
    )
    activity = json.loads(pkg.files["activity.json"])
    assert (
        activity[0]["action"] == "tenant_created"
        and activity[0]["detail"] == f"[client {alpha}] by [user] from [ip]"
        and "user" not in activity[0]
    )
    connections = json.loads(pkg.files["connections.json"])
    assert (
        connections[0]["has_client_id"] is True and "client_id" not in connections[0] and connections[0]["tenants"] == 2
    )
    assert f"[client {alpha}] page failed for [user] <[email]>" in pkg.files["logs/web.log"].decode()
    assert "Flask==" in pkg.files["packages.txt"].decode()
    assert json.loads(pkg.files["health.json"])["checks"]["worker"]["fix"]

    # None of the secrets, names or addresses.
    for secret in (
        cfg.secret_key,
        cfg.encryption_keys[0],
        world["key"],
        world["thumb"],
        "BEGIN CERTIFICATE",
        "topsecret",
        TOKEN,
        GUID,
        "Alpha Ltd",
        "Bravo Inc",
        "alpha.onmicrosoft.com",
        "jack",
        "adele",
        "finance@",
        "203.0.113.9",
        "Northwind",
        "gAAAAAB",
        "/home/jack",
    ):
        assert secret not in everything, secret
    assert world["cert"].splitlines()[1] not in everything
    assert "CLIENT IDENTIFIERS INCLUDED" not in pkg.files["README.txt"].decode()


def test_package_keeps_identifiers_only_when_asked(cfg, db, two_tenants):
    _world(db, cfg, two_tenants)
    alpha, _ = two_tenants
    pkg = support.build(db, cfg, include_identifiers=True, now=NOW)
    clients = {c["id"]: c for c in json.loads(pkg.files["clients.json"])}
    assert clients[alpha]["name"] == "Alpha Ltd" and clients[alpha]["tenant_ref"] == "alpha.onmicrosoft.com"
    failures = json.loads(pkg.files["failures.json"])
    assert f"Alpha Ltd refused the app (tenant {GUID})" in failures["runs"][0]["error"]
    assert json.loads(pkg.files["activity.json"])[0]["user"] == "jack"
    assert json.loads(pkg.files["connections.json"])[0]["client_id"] == "11111111-2222-3333-4444-555555555555"
    assert pkg.files["README.txt"].decode().splitlines()[2].startswith("CLIENT IDENTIFIERS INCLUDED")
    everything = _bytes(pkg).decode()
    # Still never a secret or an address.
    for secret in (cfg.secret_key, cfg.encryption_keys[0], TOKEN, "adele.vance@", "finance@", "203.0.113.9", "gAAAAAB"):
        assert secret not in everything, secret


def test_health_checks_and_the_portal_address_are_swept_too(cfg, db, two_tenants, monkeypatch):
    """A night where every client failed: the collection check's fix quotes the most common error, and the
    backup check quotes the backup's. Both are swept like the failures are, and so is the portal's address."""
    from officesentry import backups

    alpha, bravo = two_tenants
    monkeypatch.setenv("OFFICESENTRY_BASE_URL", "https://portal.northwind-it.co.uk")
    monkeypatch.setenv("OFFICESENTRY_DOMAIN", "reports.northwind-it.co.uk")
    monkeypatch.setenv("OFFICESENTRY_PORTAL_IPS", "198.51.100.0/24 2001:db8::/32")
    cfg.base_url = "https://portal.northwind-it.co.uk"
    slot = "2026-10-10T02:00:00Z"  # the default 02:00 collection before NOW
    error = f"Sign-in failed for Alpha Ltd ({GUID}, alpha.onmicrosoft.com): adele.vance@alpha.onmicrosoft.com"
    for tenant in (alpha, bravo):
        for collector in ("users", "mfa"):
            db.execute(
                "INSERT INTO runs (tenant_id, collector, status, trigger, scheduled_for, created_at, finished_at,"
                " error) VALUES (?, ?, 'failed', 'schedule', ?, ?, ?, ?)",
                (tenant, collector, slot, slot, slot, f"{error} from 203.0.113.9"),
            )
    backups.record_status(
        cfg.data_dir, "offsite", ok=False, error="unable to open s3:https://203.0.113.7/bucket: password=hunter2"
    )

    pkg = support.build(db, cfg, now=NOW)
    health = json.loads(pkg.files["health.json"])
    assert "[client" in health["checks"]["collection"]["fix"] and health["checks"]["backup"]["fix"]
    assert json.loads(pkg.files["environment.json"])["effective"]["base_url"] == "https://[portal]"
    everything = _bytes(pkg).decode()
    for secret in (
        GUID,
        "Alpha Ltd",
        "alpha.onmicrosoft.com",
        "adele.vance",
        "203.0.113",
        "hunter2",
        "northwind-it",
        "198.51.100",
        "2001:db8",
    ):
        assert secret not in everything, secret


def test_names_a_client_or_account_had_before_are_hidden_too(cfg, client, db, two_tenants, connection_id):
    """The activity log keeps a renamed client's old name and ID, a deleted client's name and accounts, and a
    deleted account's name. The Redactor learns them from it, as it does today's names. The data folder's
    path is swept too, since outside Docker it names the server's own user."""
    alpha, bravo = two_tenants
    make_user(db, "admin")
    gone = make_user(db, "temp.engineer", role="analyst", all_tenants=True)
    bravo_account = make_user(db, "finance.bravo", role="client", tenants=[bravo])
    login(client, "admin")
    form = {"name": "Alpha Group", "tenant_ref": "alphagroup.co.uk", "connection_id": connection_id}
    client.post(f"/tenants/{alpha}/edit", data=form)
    assert store.get_tenant(db, alpha)["tenant_ref"] == "alphagroup.co.uk"
    form = {"confirm_name": "Bravo Inc", "delete_users": bravo_account}
    client.post(f"/tenants/{bravo}/delete", data=form)
    client.post(f"/admin/users/{gone}/delete")
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1 and not store.get_tenant(db, bravo)
    logs.setup(cfg.data_dir, "web")
    logging.getLogger("officesentry.test").error("Alpha Ltd, Bravo Inc and temp.engineer were here")

    pkg = support.build(db, cfg, now=NOW)
    everything = _bytes(pkg).decode()
    assert f"[client {alpha}]" in pkg.files["activity.json"].decode()
    for name in ("Alpha Ltd", "Alpha Group", "alphagroup.co.uk", "Bravo Inc", "finance.bravo", "temp.engineer"):
        assert name not in everything, name
    elsewhere = support.build(db, cfg, data_dir=Path("/home/northwind/officesentry/data"), now=NOW)
    assert json.loads(elsewhere.files["summary.json"])["data_dir"] == "/home/[user]/officesentry/data"


def test_zip_opens_with_the_readme_first(cfg, db, two_tenants):
    pkg = support.build(db, cfg, now=NOW)
    data = support.as_zip(pkg)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        assert names[0] == "officesentry-support-20261010-220000/README.txt"
        assert all(n.startswith("officesentry-support-20261010-220000/") for n in names)
        assert set(names) == {f"{pkg.folder}/{n}" for n in pkg.files}
        readme = zf.read(names[0]).decode()
    assert "What's inside" in readme and "What's never inside" in readme and "summary.json" in readme


def test_a_database_that_wont_open_still_gives_logs_and_environment(cfg, tmp_path):
    logs.setup(cfg.data_dir, "worker")
    logging.getLogger("officesentry.test").error("the worker fell over")
    pkg = support.build(None, cfg, now=NOW)
    assert "the worker fell over" in pkg.files["logs/worker.log"].decode()
    assert json.loads(pkg.files["summary.json"])["database_error"] == "the database couldn't be opened"
    assert {p.name for p in pkg.problems} == {
        "database.json",
        "clients.json",
        "failures.json",
        "worker.json",
        "connections.json",
        "activity.json",
    }
    assert "health.json" in pkg.files and json.loads(pkg.files["health.json"]) is None
    readme = pkg.files["README.txt"].decode()
    assert "NOT MADE: Couldn't be made: the database couldn't be opened" in readme


def test_one_part_failing_never_loses_the_rest(cfg, db, two_tenants, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("no room for Alpha Ltd")

    monkeypatch.setattr(support, "_failures", broken)
    pkg = support.build(db, cfg, now=NOW)
    (problem,) = pkg.problems
    assert (
        problem.name == "failures.json" and problem.note == f"Couldn't be made: no room for [client {two_tenants[0]}]"
    )
    assert "failures.json" not in pkg.files and "clients.json" in pkg.files


# -- Settings → Support -------------------------------------------------------------------------------


def test_support_page_is_for_admins(client, db, two_tenants):
    make_user(db, "analyst", role="analyst", all_tenants=True)
    login(client, "analyst")
    assert client.get("/admin/support").status_code == 403
    assert client.post("/admin/support/package").status_code == 403


def test_support_page_leads_with_the_state_and_lists_the_package(cfg, client, db, two_tenants):
    _world(db, cfg, two_tenants)
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/admin/support").get_data(as_text=True)
    assert "Background worker: Not running" in page  # the worst check first
    assert "Failed collections" in page and ">1<" in page
    assert "Download support package" in page and "Include client identifiers" in page
    for title, _ in support.PARTS:
        assert title in page
    assert "issues/new?template=bug.yml" in page and "security/advisories/new" in page
    assert "Support" in client.get("/admin/status").get_data(as_text=True)  # linked from System status


def test_downloading_the_package_audits_and_keeps_nothing(cfg, client, db, two_tenants):
    _world(db, cfg, two_tenants)
    make_user(db, "admin")
    login(client, "admin")
    before = {p for p in cfg.data_dir.rglob("*")}
    resp = client.post("/admin/support/package")
    assert resp.status_code == 200 and resp.mimetype == "application/zip"
    assert resp.headers["Content-Disposition"].startswith('attachment; filename="officesentry-support-')
    assert resp.headers["Cache-Control"] == "no-store"
    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        everything = b"\n".join(zf.read(n) for n in zf.namelist()).decode()
    assert "Alpha Ltd" not in everything and "clients as numbers" not in everything
    assert {p for p in cfg.data_dir.rglob("*")} == before  # nothing left on disk
    row = db.execute("SELECT username, detail FROM audit_log WHERE action = 'support_package_created'").fetchone()
    assert (row["username"], row["detail"]) == ("admin", "clients as numbers")

    resp = client.post("/admin/support/package", data={"identifiers": "1"})
    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        everything = b"\n".join(zf.read(n) for n in zf.namelist()).decode()
    assert "Alpha Ltd" in everything
    assert (
        db.execute("SELECT detail FROM audit_log WHERE action = 'support_package_created' ORDER BY id DESC").fetchone()[
            0
        ]
        == "with client identifiers"
    )


# -- The command line --------------------------------------------------------------------------------


def test_command_writes_the_zip(cfg, db, two_tenants, tmp_path, monkeypatch, capsys):
    _world(db, cfg, two_tenants)
    monkeypatch.setenv("OFFICESENTRY_DATA_DIR", str(cfg.data_dir))
    out = tmp_path / "support.zip"
    main(["support-package", "--out", str(out)])
    assert f"Wrote {out}" in capsys.readouterr().out
    with zipfile.ZipFile(out) as zf:
        assert any(n.endswith("/failures.json") for n in zf.namelist())
        everything = b"\n".join(zf.read(n) for n in zf.namelist()).decode()
    assert "Alpha Ltd" not in everything and TOKEN not in everything


def test_command_writes_to_standard_output_for_docker(cfg, db, two_tenants, monkeypatch, capsysbinary):
    monkeypatch.setenv("OFFICESENTRY_DATA_DIR", str(cfg.data_dir))
    main(["support-package", "--out", "-"])
    out, err = capsysbinary.readouterr()
    assert out.startswith(b"PK") and b"standard output" in err
    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        assert zf.namelist()[0].endswith("/README.txt")


def test_command_works_over_a_broken_database(cfg, tmp_path, monkeypatch, capsys):
    cfg.db_path.write_bytes(b"this is not a database" * 100)
    monkeypatch.setenv("OFFICESENTRY_DATA_DIR", str(cfg.data_dir))
    out = tmp_path / "support.zip"
    main(["support-package", "--out", str(out), "--include-identifiers"])
    captured = capsys.readouterr()
    assert "database couldn't be opened" in captured.err and "private channel only" in captured.out
    assert "clients.json: Couldn't be made" in captured.out
    with zipfile.ZipFile(out) as zf:
        summary = json.loads(zf.read(next(n for n in zf.namelist() if n.endswith("summary.json"))))
    assert summary["database_error"] == "the database couldn't be opened" and summary["database_file_bytes"] == 2200


def test_command_works_when_the_configuration_wont_load(cfg, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OFFICESENTRY_DATA_DIR", str(cfg.data_dir))
    monkeypatch.setenv("OFFICESENTRY_TIMEZONE", "Mars/Olympus")
    out = tmp_path / "support.zip"
    main(["support-package", "--out", str(out)])
    assert "configuration didn't load" in capsys.readouterr().err
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        summary = json.loads(zf.read(next(n for n in names if n.endswith("summary.json"))))
        env = json.loads(zf.read(next(n for n in names if n.endswith("environment.json"))))
    assert "OFFICESENTRY_TIMEZONE" in summary["config_error"] and env["effective"] is None
    assert env["variables"]["OFFICESENTRY_TIMEZONE"] == "Mars/Olympus"


# -- Logging: files, request ids and the worker's context ----------------------------------------------


def test_the_portal_keeps_a_rolling_log_and_names_the_request(cfg, db, two_tenants, monkeypatch):
    app = create_app(cfg)
    app.config["PROPAGATE_EXCEPTIONS"] = False

    @app.get("/tenants/<int:tenant_id>/boom")
    def boom(tenant_id):
        raise RuntimeError("kaboom")

    alpha, _ = two_tenants
    make_user(db, "admin")
    client = app.test_client()
    login(client, "admin")
    ok = client.get("/admin/status")
    assert len(ok.headers["X-Request-ID"]) == 12

    resp = client.get(f"/tenants/{alpha}/boom")
    assert resp.status_code == 500
    ref = resp.headers["X-Request-ID"]
    page = resp.get_data(as_text=True)
    assert "Something went wrong on our side." in page and ref in page and "/admin/support" in page
    text = logs.log_file(cfg.data_dir, "web").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if "kaboom" in ln or "Exception on" in ln)
    assert f" req={ref} user=admin tenant={alpha}: " in line and " ERROR " in line
    assert "RuntimeError: kaboom" in text
    assert logs.recent_errors(cfg.data_dir) == {"web": 1, "worker": 0}
    # Signed out, the error page shows the reference but not the admin's link.
    client.post("/logout")
    client.get("/login")
    assert logs.current_context() == {}  # reset after every request


def test_log_files_rotate_and_the_tail_crosses_them(cfg, monkeypatch):
    monkeypatch.setattr(logs, "LOG_BYTES", 2000)
    logs.setup(cfg.data_dir, "worker")
    for n in range(60):
        logging.getLogger("officesentry.test").info("line %03d %s", n, "x" * 60)
    assert len(logs.files(cfg.data_dir, "worker")) == logs.LOG_FILES
    tail = logs.tail(cfg.data_dir, "worker", 3000)
    assert "line 059" in tail and tail.startswith("20") and 2900 <= len(tail) <= 3000  # whole lines, newest last
    assert logs.sizes(cfg.data_dir)["worker"] <= logs.LOG_FILES * 2100
    assert logs.tail(cfg.data_dir, "web", 1000) == ""


def test_making_a_pdf_leaves_the_log_to_office_sentry(cfg):
    pytest.importorskip("weasyprint")
    from officesentry.reports import export

    logs.setup(cfg.data_dir, "worker")
    page = "<html><head><style>svg { fill: red } @media (max-width: 9px) { p { color: red } }</style></head>"
    static = Path(export.__file__).resolve().parents[1] / "web" / "static"
    export.to_pdf(page + "<body><p>Hello</p><svg><circle r='1'/></svg></body></html>", static.as_uri() + "/")
    logging.getLogger("weasyprint").error("Failed to load image at %s", "static/missing.png")
    text = logs.log_file(cfg.data_dir, "worker").read_text(encoding="utf-8")
    # WeasyPrint's progress, its CSS notes and fontTools' subsetting stayed out; its errors still come through.
    assert text.splitlines() == [ln for ln in text.splitlines() if "ERROR weasyprint" in ln]
    assert "Failed to load image at static/missing.png" in text


def test_setup_survives_an_unwritable_folder(cfg, tmp_path, caplog):
    blocked = tmp_path / "file"
    blocked.write_text("not a folder", encoding="utf-8")
    logs.setup(blocked, "web")  # logs/ can't be made under a file
    assert "Not keeping a log file" in caplog.text


def test_worker_lines_name_the_run_and_task(cfg, db, two_tenants):
    logs.setup(cfg.data_dir, "worker")
    alpha, _ = two_tenants
    run_id = store.enqueue_run(db, alpha, "users")
    run = store.claim_next_run(db)

    def factory(tenant, connection):
        raise RuntimeError("certificate for Alpha Ltd was refused")

    worker._execute(db, run, factory, None, None, worker.Liveness())
    text = logs.log_file(cfg.data_dir, "worker").read_text(encoding="utf-8")
    assert f" run={run_id} tenant={alpha} collector=users: Run started" in text
    assert f" run={run_id} tenant={alpha} collector=users: Run failed on a bug (RuntimeError)" in text
    assert db.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()[0] == "failed"

    @tasks.handler("support_test")
    def fail(ctx):
        raise tasks.TaskFailed("nothing to approve")

    task_id = tasks.enqueue(db, "support_test", tenant_id=alpha)
    task = tasks.claim_next(db)
    with logs.context(task=task["id"], kind=task["kind"], tenant=task["tenant_id"]):
        tasks.run(None, db, None, cfg, task)
    text = logs.log_file(cfg.data_dir, "worker").read_text(encoding="utf-8")
    assert (
        f" task={task_id} kind=support_test tenant={alpha}: Task {task_id} (support_test) failed: nothing to approve"
        in text
    )
    tasks.HANDLERS.pop("support_test", None)
