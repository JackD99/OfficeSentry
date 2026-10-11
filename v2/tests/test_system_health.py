"""Office Sentry's own health: /health/ready, the System status page and system alerts."""

import json
import socket
from datetime import UTC, datetime, timedelta

import pytest
import responses

from officesentry import alerts, backups, delivery, mailer, schedule, system_alerts, system_health, worker
from officesentry.__main__ import main
from officesentry.db import latest_version
from officesentry.web import create_app
from tests.conftest import login, make_user

HOOK = "https://hooks.example.com/workflows/abc?sig=TOPSECRETSIG"
SEND_MAIL = "https://graph.microsoft.com/v1.0/users/reports@msp.com/sendMail"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _beat(db, at: datetime) -> None:
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (worker.HEARTBEAT_KEY, _iso(at)),
    )


def _disk(monkeypatch, used: float) -> None:
    monkeypatch.setattr(system_health, "disk_usage", lambda path: (used, int((100 - used) * 2**30), 100 * 2**30))


def _run(db, tenant_id, status, collector="users", slot=None, created=None, error=None):
    created = created or _iso(NOW - timedelta(hours=9))
    return db.execute(
        "INSERT INTO runs (tenant_id, collector, status, trigger, scheduled_for, created_at,"
        " finished_at, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tenant_id,
            collector,
            status,
            "schedule" if slot else "manual",
            slot,
            created,
            None if status in ("queued", "running") else created,
            error,
        ),
    ).lastrowid


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.delenv(alerts.ALLOW_PRIVATE_ENV, raising=False)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("52.1.2.3", port))],
    )


@pytest.fixture
def channels(db, vault, public_dns, monkeypatch):
    """Email to the team (through Microsoft 365) and a generic webhook, both set up."""
    for key, value in {
        "mail_transport": "graph",
        "graph_auth": "secret",
        "graph_tenant": "msp.onmicrosoft.com",
        "graph_client_id": "11111111-2222-3333-4444-555555555555",
        "graph_secret_enc": vault.encrypt("shh"),
        "graph_sender": "reports@msp.com",
    }.items():
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
    monkeypatch.setattr(mailer, "_graph_token", lambda s, v: "mail-token")
    delivery.add_contact(db, None, "Ops", "ops@msp.com")
    alerts.save_settings(db, {"alerts.enabled": "1", "alerts.format": "generic", "alerts.url_enc": vault.encrypt(HOOK)})


def _sent(rsps):
    mails = [json.loads(c.request.body)["message"] for c in rsps.calls if "sendMail" in c.request.url]
    hooks = [json.loads(c.request.body) for c in rsps.calls if c.request.url.startswith("https://hooks.")]
    return mails, hooks


# -- /health/ready ---------------------------------------------------------------------------------


def test_anyone_sees_only_whether_it_is_ready(client, db, two_tenants):
    resp = client.get("/health/ready")
    assert resp.status_code == 503 and resp.json == {"ok": False}  # the worker has never checked in
    worker.beat(db)
    resp = client.get("/health/ready")
    assert resp.status_code == 200 and resp.json == {"ok": True}
    assert resp.headers["Cache-Control"] == "no-store"
    assert client.get("/health/ready?token=wrong").json == {"ok": True}
    assert client.get("/health/ready", headers={"Authorization": "Bearer wrong"}).json == {"ok": True}


def test_the_token_only_works_as_a_header(client, db):
    """In the address it would end up in proxy and monitor logs, so there it's ignored."""
    worker.beat(db)
    token = system_health.new_token(db)
    assert client.get(f"/health/ready?token={token}").json == {"ok": True}
    assert "checks" in client.get("/health/ready", headers={"Authorization": f"Bearer {token}"}).json


def test_the_token_or_an_admin_sees_every_check(client, db, two_tenants):
    worker.beat(db)
    token = system_health.new_token(db)
    stored = db.execute("SELECT value FROM settings WHERE key = ?", (system_health.TOKEN_KEY,)).fetchone()[0]
    assert token not in stored and len(stored) == 64  # only its hash
    resp = client.get("/health/ready", headers={"Authorization": f"Bearer {token}"})
    body = resp.json
    assert resp.status_code == 200 and body["ok"] is True and body["status"] in ("ok", "warn")
    assert set(body["checks"]) == {
        "database",
        "schema",
        "worker",
        "collection",
        "backup",
        "disk",
        "certificates",
        "erase",
    }
    assert body["checks"]["schema"]["version"] == latest_version()
    assert body["checks"]["worker"]["status"] == "ok" and body["checks"]["worker"]["age_seconds"] < 60
    assert body["checks"]["disk"]["used_percent"] == 40.0
    assert body["checks"]["collection"]["tenants"] == 2
    text = json.dumps(body)
    assert "Alpha" not in text and "alpha.onmicrosoft.com" not in text and token not in text
    system_health.remove_token(db)
    assert client.get("/health/ready", headers={"Authorization": f"Bearer {token}"}).json == {"ok": True}
    make_user(db, "admin")
    login(client, "admin")
    assert "checks" in client.get("/health/ready").json


def test_an_analyst_session_does_not_see_the_details(client, db, two_tenants):
    worker.beat(db)
    make_user(db, "analyst", role="analyst", all_tenants=True)
    login(client, "analyst")
    assert client.get("/health/ready").json == {"ok": True}


def test_a_full_disk_or_a_database_from_another_release_is_not_ready(client, cfg, db, monkeypatch):
    worker.beat(db)
    _disk(monkeypatch, 90)
    health = system_health.assess(db, cfg)
    assert health.ok and health.check("disk").status == "warn"  # needs attention, still works
    assert client.get("/health/ready").status_code == 200
    _disk(monkeypatch, 96)
    assert client.get("/health/ready").status_code == 503
    _disk(monkeypatch, 40)
    db.execute(f"PRAGMA user_version = {latest_version() + 1}")
    check = system_health.assess(db, cfg).check("schema")
    assert check.status == "fail" and "newer" in check.summary
    assert client.get("/health/ready").status_code == 503


def test_a_check_that_breaks_is_reported_not_raised(cfg, db, monkeypatch):
    def broken(*a):
        raise OSError("gone")

    monkeypatch.setattr(system_health, "disk_usage", broken)
    check = system_health.assess(db, cfg).check("disk")
    assert check.status == "fail" and check.summary == "Couldn't be checked (OSError)"


# -- Backups -----------------------------------------------------------------------------------------


def test_backup_status(cfg, db):
    check = system_health.check_backup(db, cfg, NOW)
    assert check.status == "warn" and check.summary == "No backup has been made yet"
    assert check.data["configured"] is False

    backups.record_status(cfg.data_dir, "local", True, now=NOW - timedelta(hours=5))
    check = system_health.check_backup(db, cfg, NOW)
    assert check.status == "ok" and check.summary == "Last backup 5 hours ago"

    backups.record_status(cfg.data_dir, "local", False, "No backup made: No space left on device.", now=NOW)
    check = system_health.check_backup(db, cfg, NOW)
    assert check.status == "warn" and check.summary == "The last backup failed"
    assert check.fix.startswith("No backup made: No space left on device.")
    assert check.data["jobs"]["local"]["last_success"] == _iso(NOW - timedelta(hours=5))  # kept

    backups.record_status(cfg.data_dir, "local", True, now=NOW - timedelta(hours=40))
    assert system_health.check_backup(db, cfg, NOW).summary == "No backup for 40 hours"

    # Another job (an off-site copy, say) writes its own entry in the same file.
    backups.record_status(cfg.data_dir, "local", True, now=NOW)
    status = json.loads((cfg.data_dir / backups.STATUS_FILE).read_text(encoding="utf-8"))
    status["offsite"] = {"ok": False, "last_attempt": _iso(NOW), "last_success": None, "detail": "", "error": "403"}
    (cfg.data_dir / backups.STATUS_FILE).write_text(json.dumps(status), encoding="utf-8")
    check = system_health.check_backup(db, cfg, NOW)
    assert check.summary == "The last off-site backup failed" and check.fix.startswith("403.")
    assert "backup action" in check.fix and "docker compose exec" not in check.fix
    assert status["local"]["detail"] == "" and status["local"]["error"] == ""  # the deployment format (WS12)

    # The weekly restore drill may be 8 days old; a nightly copy only 26 hours.
    status["offsite"] = {"ok": True, "last_attempt": _iso(NOW), "last_success": _iso(NOW), "detail": "", "error": ""}
    status["restore_check"] = {
        "ok": True,
        "last_attempt": _iso(NOW - timedelta(days=6)),
        "last_success": _iso(NOW - timedelta(days=6)),
        "detail": "",
        "error": "",
    }
    (cfg.data_dir / backups.STATUS_FILE).write_text(json.dumps(status), encoding="utf-8")
    assert system_health.check_backup(db, cfg, NOW).summary == "Last backup 0 seconds ago"
    assert system_health.check_backup(db, cfg, NOW + timedelta(hours=27)).summary == "No backup for 27 hours"
    status["local"]["last_success"] = status["offsite"]["last_success"] = _iso(NOW + timedelta(days=3))
    (cfg.data_dir / backups.STATUS_FILE).write_text(json.dumps(status), encoding="utf-8")
    check = system_health.check_backup(db, cfg, NOW + timedelta(days=3))
    assert check.summary == "No restore check for 9 days"
    backups.record_status(
        cfg.data_dir,
        "restore_check",
        False,
        "the copy didn't open",
        detail="officesentry-20261009-034500.db.gz",
        now=NOW + timedelta(days=3),
    )
    check = system_health.check_backup(db, cfg, NOW + timedelta(days=3))
    assert check.summary == "The last restore check failed" and "restore-drill" in check.fix
    stored = backups.read_status(cfg.data_dir)
    assert stored["restore_check"]["detail"] == "officesentry-20261009-034500.db.gz"
    backups.record_status(cfg.data_dir, "local", True, now=NOW)  # another job's record keeps it
    assert backups.read_status(cfg.data_dir)["restore_check"]["detail"] == "officesentry-20261009-034500.db.gz"


def test_off_site_backups_turned_off_stop_warning(cfg, db, capsys, monkeypatch):
    monkeypatch.setenv("OFFICESENTRY_DATA_DIR", str(cfg.data_dir))
    backups.record_status(cfg.data_dir, "local", True, now=NOW)
    backups.record_status(cfg.data_dir, "offsite", True, now=NOW - timedelta(days=10))  # from when it was set up
    assert system_health.check_backup(db, cfg, NOW).summary == "No off-site backup for 10 days"
    # The nightly job finds RESTIC_REPOSITORY gone and says so (deploy/jobs.sh, offsite_copy).
    main(["backup-status", "--record", "offsite", "--off", "--detail", "not set up"])
    check = system_health.check_backup(db, cfg, NOW)
    assert check.status == "ok" and "offsite" not in check.data["jobs"]
    entry = backups.read_status(cfg.data_dir)["offsite"]
    assert entry["off"] is True and entry["ok"] is None and entry["last_success"] == _iso(NOW - timedelta(days=10))
    capsys.readouterr()
    main(["backup-status"])
    assert "Off-site copy: not set up, so skipped at" in capsys.readouterr().out
    # Set up again: its next result counts as before.
    main(["backup-status", "--record", "offsite", "--failed", "--error", "wrong password"])
    assert not backups.read_status(cfg.data_dir)["offsite"]["off"]
    assert system_health.check_backup(db, cfg, NOW).summary == "The last off-site backup failed"
    with pytest.raises(SystemExit, match="--ok, --failed or --off"):
        main(["backup-status", "--record", "offsite", "--ok", "--off"])


def test_the_jobs_script_records_skipped_off_site_steps():
    from pathlib import Path

    script = (Path(system_health.__file__).parent.parent / "deploy" / "jobs.sh").read_text(encoding="utf-8")
    offsite = script[script.index("offsite_copy() {") :]
    drill = script[script.index("drill() {") :]
    assert 'record offsite off "not set up"' in offsite[: offsite.index("open_repository")]
    assert "record restore_check off" in drill[: drill.index("open_repository")]
    # The uptime ping asks the deep check, so a dead worker or a full disk fails it too.
    ping = script[script.index("uptime_ping() {") :]
    assert "http://127.0.0.1:8000/health/ready;" in ping[: ping.index("schedule() {")]


def test_backups_from_before_the_status_file_count(cfg, db):
    folder = cfg.data_dir / "backups"
    folder.mkdir(parents=True)
    (folder / "officesentry-20261009-033000.db.gz").write_bytes(b"x")
    check = system_health.check_backup(db, cfg, datetime.now(UTC))
    assert check.status == "ok" and check.data["configured"] is True


def test_the_backup_command_records_its_outcome(cfg, db, monkeypatch, capsys):
    monkeypatch.setenv("OFFICESENTRY_DATA_DIR", str(cfg.data_dir))
    main(["backup"])
    local = backups.read_status(cfg.data_dir)["local"]
    assert local["ok"] is True and local["detail"].startswith("officesentry-") and local["detail"].endswith(" MB")
    assert local["error"] == "" and local["last_success"] == local["last_attempt"]
    assert oct((cfg.data_dir / backups.STATUS_FILE).stat().st_mode & 0o777) == "0o600"

    def full(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(backups, "backup", full)
    with pytest.raises(SystemExit):
        main(["backup"])
    local = backups.read_status(cfg.data_dir)["local"]
    assert local["ok"] is False and "No space left" in local["error"] and local["last_success"]


def test_a_damaged_status_file_reads_as_none(cfg, db):
    cfg.data_dir.joinpath(backups.STATUS_FILE).write_text("{not json", encoding="utf-8")
    assert backups.read_status(cfg.data_dir) == {}
    backups.record_status(cfg.data_dir, "local", True)  # and is replaced by the next backup
    assert backups.read_status(cfg.data_dir)["local"]["ok"] is True


# -- Collection --------------------------------------------------------------------------------------


def test_most_tenants_failing_the_last_slot(cfg, db, two_tenants, connection_id):
    from officesentry import store

    alpha, bravo = two_tenants
    charlie = store.create_tenant(db, "Charlie", "charlie.onmicrosoft.com", connection_id)
    db.execute("UPDATE tenants SET consented_at = '2026-01-01T00:00:00Z'")
    _run(db, alpha, "succeeded", created=_iso(NOW - timedelta(days=1, hours=9)))  # the night before
    slot = _iso(NOW - timedelta(hours=10))
    for tenant in (alpha, bravo):
        for collector in ("users", "mfa", "devices"):
            _run(db, tenant, "failed", collector, slot, error="Sign-in failed: the certificate has expired\nmore")
    _run(db, charlie, "succeeded", "users", slot)
    _run(db, charlie, "failed", "mfa", slot, error="Exchange needs Global Reader")  # one of two: not failed
    _run(db, charlie, "succeeded", "devices", slot)

    result = system_health.last_slot(db, cfg, NOW, NOW - timedelta(days=2))
    assert (result.tenants, result.failed, result.pending) == (3, 2, 0) and result.mass_failure
    assert result.top_error == "Sign-in failed: the certificate has expired"
    check = system_health.check_collection(db, cfg, NOW)
    assert check.status == "warn" and check.summary == "The last scheduled collection failed for 2 of 3 tenants"
    assert "certificate has expired" in check.fix
    assert "expired" not in json.dumps(check.data)  # errors stay out of the JSON

    queued = _run(db, charlie, "queued", "teams", slot)
    assert not system_health.last_slot(db, cfg, NOW, NOW - timedelta(days=2)).mass_failure  # not finished yet
    db.execute("DELETE FROM runs WHERE id = ?", (queued,))
    assert system_health.last_slot(db, cfg, NOW, NOW) is None  # nothing newer than now


def test_a_report_refresh_does_not_hide_a_night_every_tenant_failed(cfg, db, two_tenants):
    # Every tenant failed at 02:00 and the alert went out. Then Alpha's report fell due at 08:00 and its
    # refresh runs, scheduled for 08:00, failed for the same reason: one tenant, one failure. That's not the
    # last collection slot, so the warning stays.
    alpha, bravo = two_tenants
    _run(db, alpha, "succeeded", created=_iso(NOW - timedelta(days=1, hours=9)))  # the night before
    night = _iso(NOW - timedelta(hours=10))
    for tenant in (alpha, bravo):
        for collector in ("users", "mfa"):
            _run(db, tenant, "failed", collector, night, error="Sign-in failed: the certificate has expired")
    report_due = NOW - timedelta(hours=4)
    for collector in ("users", "mfa"):
        _run(db, alpha, "failed", collector, _iso(report_due), created=_iso(report_due), error="Sign-in failed")
    check = system_health.check_collection(db, cfg, NOW)
    assert check.status == "warn" and check.summary == "The last scheduled collection failed for 2 of 2 tenants"
    assert check.data["last_slot"]["at"] == night


def test_a_slot_that_queued_nothing_falls_back_to_the_one_before(cfg, db, two_tenants):
    alpha, bravo = two_tenants
    night_before = _iso(NOW - timedelta(days=1, hours=10))
    for tenant in (alpha, bravo):
        _run(db, tenant, "failed", "users", night_before, created=night_before, error="Sign-in failed")
    slot = system_health.last_slot(db, cfg, NOW, NOW - timedelta(days=2))  # last night's queued nothing
    assert slot.at == night_before and slot.mass_failure


def test_collection_ok_and_stale(cfg, db, two_tenants):
    alpha, _ = two_tenants
    assert system_health.check_collection(db, cfg, NOW).summary == "Nothing has been collected yet"
    _run(db, alpha, "succeeded", created=_iso(NOW - timedelta(hours=9)))
    check = system_health.check_collection(db, cfg, NOW)
    assert check.status == "ok" and check.summary == "Last data collected 9 hours ago"
    check = system_health.check_collection(db, cfg, NOW + timedelta(days=2))
    assert check.status == "warn" and check.summary == "Nothing collected for 2 days"
    db.execute("UPDATE tenants SET enabled = 0")
    assert system_health.check_collection(db, cfg, NOW).summary == "No client tenants to collect yet"


def test_a_weekly_schedule_allows_a_week(cfg, db):
    cfg.collect_cron = "0 2 * * 1"
    assert system_health.collection_interval(cfg, NOW) == timedelta(days=7, hours=12)


# -- System alerts -----------------------------------------------------------------------------------


def test_worker_down_alerts_once_reminds_daily_and_says_when_it_is_back(cfg, db, vault, channels):
    app = create_app(cfg)
    _beat(db, NOW - timedelta(minutes=4))
    with responses.RequestsMock() as rsps:
        rsps.post(SEND_MAIL, status=202)
        rsps.post(HOOK, status=202)
        assert system_alerts.update(app, db, vault, cfg, ("worker",), NOW) == []  # alive
        _beat(db, NOW - timedelta(minutes=25))
        assert system_alerts.update(app, db, vault, cfg, ("worker",), NOW) == [("worker", "start")]
        assert system_alerts.update(app, db, vault, cfg, ("worker",), NOW + timedelta(hours=1)) == []
        assert system_alerts.update(app, db, vault, cfg, ("worker",), NOW + timedelta(hours=24)) == [
            ("worker", "reminder")
        ]
        _beat(db, NOW + timedelta(hours=25))
        assert system_alerts.update(app, db, vault, cfg, ("worker",), NOW + timedelta(hours=25)) == [
            ("worker", "resolved")
        ]
        mails, hooks = _sent(rsps)
    assert [h["event"] for h in hooks] == ["system.worker", "system.worker", "system.worker.resolved"]
    assert hooks[0]["title"] == "Office Sentry's background worker has stopped"
    assert hooks[0]["url"] == cfg.base_url.rstrip("/") + "/admin/status"
    assert hooks[0]["system"]["state"] == "start" and hooks[1]["title"].startswith("Still not fixed: ")
    assert "It last checked in 25 minutes ago." in hooks[0]["text"] and "docker compose up -d" in hooks[0]["text"]
    assert [m["subject"] for m in mails] == [
        "Action needed: Office Sentry's background worker has stopped",
        "Action needed: Still not fixed: Office Sentry's background worker has stopped",
        "Resolved: Office Sentry's background worker is running again",
    ]
    assert [r["emailAddress"]["address"] for r in mails[0]["toRecipients"]] == ["ops@msp.com"]
    assert "Open System status" in mails[0]["body"]["content"]
    assert system_alerts.states(db) == {}
    rows = db.execute("SELECT detail FROM audit_log WHERE action = 'system_alert' ORDER BY id").fetchall()
    assert [r[0] for r in rows] == [
        "Worker stopped, start: email sent; webhook sent",
        "Worker stopped, reminder: email sent; webhook sent",
        "Worker stopped, resolved: email sent; webhook sent",
    ]


def test_a_restart_does_not_alert(cfg, db, vault, channels):
    """A worker that stopped cleanly clears its heartbeat; it gets ten minutes to come back."""
    with responses.RequestsMock() as rsps:
        assert system_alerts.update(None, db, vault, cfg, ("worker",), NOW) == []
        assert system_alerts.states(db)["worker"]["sent_at"] is None
        _beat(db, NOW + timedelta(minutes=2))
        assert system_alerts.update(None, db, vault, cfg, ("worker",), NOW + timedelta(minutes=2)) == []
        assert not rsps.calls
    assert system_alerts.states(db) == {}
    with responses.RequestsMock() as rsps:
        rsps.post(HOOK, status=202)
        # It stopped again and has been gone 18 minutes since its last heartbeat.
        assert system_alerts.update(None, db, vault, cfg, ("worker",), NOW + timedelta(minutes=20)) == [
            ("worker", "start")
        ]
        assert len(rsps.calls) == 1  # webhook only: without the app there's no email to render


def test_disk_alert_and_its_margin(cfg, db, vault, channels, monkeypatch):
    with responses.RequestsMock() as rsps:
        rsps.post(HOOK, status=202)
        _disk(monkeypatch, 87)
        assert system_alerts.update(None, db, vault, cfg, ("disk",), NOW) == [("disk", "start")]
        _disk(monkeypatch, 82)  # under 85 but not under 80: still a problem, no flapping
        assert system_alerts.update(None, db, vault, cfg, ("disk",), NOW + timedelta(minutes=1)) == []
        assert "disk" in system_alerts.states(db)
        _disk(monkeypatch, 79)
        assert system_alerts.update(None, db, vault, cfg, ("disk",), NOW + timedelta(minutes=2)) == [
            ("disk", "resolved")
        ]
        _, hooks = _sent(rsps)
    assert hooks[0]["title"] == "Office Sentry's disk is 87% full" and hooks[0]["system"]["used_percent"] == 87
    assert hooks[1]["title"] == "Office Sentry's disk is under 80% full again"


def test_backup_alert_only_once_backups_are_set_up(cfg, db, vault, channels):
    with responses.RequestsMock() as rsps:
        assert system_alerts.update(None, db, vault, cfg, ("backup",), NOW) == []  # never set up: page only
        rsps.post(HOOK, status=202)
        backups.record_status(cfg.data_dir, "local", False, "No backup made: database disk image is malformed", now=NOW)
        assert system_alerts.update(None, db, vault, cfg, ("backup",), NOW) == [("backup", "start")]
        _, hooks = _sent(rsps)
    assert hooks[0]["title"] == "Office Sentry's backup needs attention: the last backup failed"
    assert "database disk image is malformed" in hooks[0]["text"]


def test_mass_failure_alert_waits_for_the_worker(cfg, db, vault, channels, two_tenants):
    alpha, bravo = two_tenants
    _run(db, alpha, "succeeded", created=_iso(NOW - timedelta(days=1)))
    slot = _iso(NOW - timedelta(hours=10))
    for tenant in (alpha, bravo):
        _run(db, tenant, "failed", "users", slot, error="Sign-in failed: the app was deleted")
    with responses.RequestsMock() as rsps:
        rsps.post(HOOK, status=202)
        assert system_alerts.update(None, db, vault, cfg, ("collection",), NOW) == []  # worker down says it
        _beat(db, NOW)
        assert system_alerts.update(None, db, vault, cfg, ("collection",), NOW) == [("collection", "start")]
        _, hooks = _sent(rsps)
    assert hooks[0]["title"] == "The last scheduled collection failed for 2 of 2 tenants"
    assert "the app was deleted" in hooks[0]["text"]


def test_an_alert_nobody_received_is_sent_again(cfg, db, vault, channels, monkeypatch):
    app = create_app(cfg)
    _disk(monkeypatch, 90)
    with responses.RequestsMock() as rsps:
        rsps.post(SEND_MAIL, status=503)
        rsps.post(HOOK, status=500)
        assert system_alerts.update(app, db, vault, cfg, ("disk",), NOW) == [("disk", "start")]
        assert system_alerts.states(db)["disk"]["sent_at"] is None  # the page says "not sent yet"
        assert system_alerts.update(app, db, vault, cfg, ("disk",), NOW + timedelta(minutes=5)) == []
    with responses.RequestsMock() as rsps:
        rsps.post(SEND_MAIL, status=202)
        rsps.post(HOOK, status=202)
        later = NOW + system_alerts.RETRY
        assert system_alerts.update(app, db, vault, cfg, ("disk",), later) == [("disk", "start")]
        assert len(_sent(rsps)[0]) == 1
    assert system_alerts.states(db)["disk"]["sent_at"] == _iso(later)
    rows = [r[0] for r in db.execute("SELECT detail FROM audit_log WHERE action = 'system_alert' ORDER BY id")]
    assert rows[0].startswith("Disk over 85% full, start: email failed") and rows[0].endswith(
        "webhook failed; trying again in 15 minutes"
    )
    assert rows[1] == "Disk over 85% full, start: email sent; webhook sent"


def test_one_channel_taking_the_alert_is_enough(cfg, db, vault, channels, monkeypatch):
    app = create_app(cfg)
    _disk(monkeypatch, 90)
    with responses.RequestsMock() as rsps:
        rsps.post(SEND_MAIL, status=503)
        rsps.post(HOOK, status=202)
        assert system_alerts.update(app, db, vault, cfg, ("disk",), NOW) == [("disk", "start")]
    assert system_alerts.states(db)["disk"]["sent_at"] == _iso(NOW)


def test_a_failed_reminder_is_tried_again(cfg, db, vault, channels, monkeypatch):
    _disk(monkeypatch, 90)
    with responses.RequestsMock() as rsps:
        rsps.post(HOOK, status=202)
        system_alerts.update(None, db, vault, cfg, ("disk",), NOW)
    with responses.RequestsMock() as rsps:
        rsps.post(HOOK, status=500)
        assert system_alerts.update(None, db, vault, cfg, ("disk",), NOW + system_alerts.REMIND) == [
            ("disk", "reminder")
        ]
    assert system_alerts.states(db)["disk"]["sent_at"] == _iso(NOW)  # still the last one that arrived
    with responses.RequestsMock() as rsps:
        rsps.post(HOOK, status=202)
        at = NOW + system_alerts.REMIND + system_alerts.RETRY
        assert system_alerts.update(None, db, vault, cfg, ("disk",), at) == [("disk", "reminder")]


def test_turned_off_sends_nothing(cfg, db, vault, channels, monkeypatch):
    alerts.save_settings(db, {"alerts.system": "0"})
    _disk(monkeypatch, 90)
    with responses.RequestsMock() as rsps:
        assert system_alerts.update(None, db, vault, cfg, ("disk",), NOW) == [("disk", "start")]
        assert not rsps.calls
    assert not db.execute("SELECT 1 FROM audit_log WHERE action = 'system_alert'").fetchone()


def test_the_worker_runs_the_job_and_the_portal_watches_the_worker(cfg, db, vault, monkeypatch):
    seen = []
    monkeypatch.setattr(system_alerts, "update", lambda app, conn, vault, cfg, keys, now=None: seen.append(keys))
    worker.housekeeping(create_app(cfg), db, vault, cfg, NOW)
    assert "system_alerts" in schedule.PERIODIC and seen == [system_alerts.WORKER_KEYS]
    assert system_alerts.start_watchdog(None, cfg) is False  # not in tests
    started = []
    monkeypatch.setattr(
        system_alerts.threading,
        "Thread",
        lambda **kw: type("T", (), {"start": lambda self: started.append(kw["name"])})(),
    )
    monkeypatch.setattr(system_alerts, "_watching", {"pid": None})
    cfg.testing = False
    assert system_alerts.start_watchdog(None, cfg) is True
    assert system_alerts.start_watchdog(None, cfg) is False  # once per process
    assert started == ["system-watchdog"]


# -- Pages ---------------------------------------------------------------------------------------------


def test_status_page_leads_with_what_needs_attention(client, db, monkeypatch, two_tenants):
    make_user(db, "admin")
    login(client, "admin")
    _disk(monkeypatch, 91)
    page = client.get("/admin/status").get_data(as_text=True)
    assert "Needs attention" in page and "Background worker: Not running" in page
    assert page.index("Background worker: Not running") < page.index("Every check")
    assert "Disk space: 91% used" in page and "Nowhere to send them yet" in page
    assert "Create monitoring token" in page and "/health/ready" in page

    worker.beat(db)
    _disk(monkeypatch, 40)
    backups.record_status(client.application.extensions["officesentry.config"].data_dir, "local", True)
    db.execute(
        "INSERT INTO runs (tenant_id, collector, status, trigger, created_at, finished_at)"
        " VALUES (1, 'users', 'succeeded', 'manual', ?, ?)",
        (_iso(datetime.now(UTC)),) * 2,
    )
    page = client.get("/admin/status").get_data(as_text=True)
    assert "Everything is working." in page and "Needs attention" not in page


def test_monitoring_token_is_shown_once(client, db):
    make_user(db, "admin")
    login(client, "admin")
    page = client.post("/admin/status/token").get_data(as_text=True)
    assert "?token=" not in page
    token = page.split('curl -H "Authorization: Bearer ')[1].split('"')[0]
    assert token.startswith("osh_") and system_health.token_matches(db, token)
    assert "/health/ready</p>" in page
    page = client.get("/admin/status").get_data(as_text=True)
    assert token not in page and "A monitoring token is set" in page
    client.post("/admin/status/token/remove")
    assert not system_health.has_token(db)
    actions = [r[0] for r in db.execute("SELECT action FROM audit_log WHERE action LIKE 'health_token%' ORDER BY id")]
    assert actions == ["health_token_created", "health_token_removed"]
    assert token not in json.dumps([dict(r) for r in db.execute("SELECT * FROM audit_log")])


def test_only_admins_open_the_status_page(client, db):
    make_user(db, "analyst", role="analyst", all_tenants=True)
    login(client, "analyst")
    assert client.get("/admin/status").status_code == 403
    assert client.post("/admin/status/token").status_code == 403


def test_alerts_page_turns_system_alerts_off(client, db):
    make_user(db, "admin")
    login(client, "admin")
    assert "Problems with Office Sentry" in client.get("/admin/alerts").get_data(as_text=True)
    client.post("/admin/alerts", data={"format": "teams", "min_severity": "high", "on_failures": "1"})
    assert alerts.settings(db)["alerts.system"] == "0"
    client.post("/admin/alerts", data={"format": "teams", "min_severity": "high", "system": "1"})
    assert alerts.settings(db)["alerts.system"] == "1"
