"""Keys kept apart from the data, the backup jobs' status, and the restore drill's check (DEPLOY.md, "Where the
keys are", "Off-site backups" and "Restore drill")."""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from officesentry import backups
from officesentry.__main__ import main
from officesentry.config import load_config
from officesentry.crypto import Vault

V2 = Path(__file__).resolve().parent.parent


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("SECRET_KEY", "ENCRYPTION_KEYS", "DATA_DIR", "SECRETS_FILE"):
        monkeypatch.delenv("OFFICESENTRY_" + var, raising=False)
    return monkeypatch


@pytest.mark.parametrize(
    ("name", "value"), [("SESSION_HOURS", "0"), ("SESSION_IDLE_MINUTES", "0"), ("TRUSTED_PROXIES", "-1")]
)
def test_session_and_proxy_settings_are_checked_at_start(clean_env, tmp_path, name, value):
    """SESSION_HOURS=0 used to start fine and sign everyone out on their next request."""
    clean_env.setenv("OFFICESENTRY_" + name, value)
    with pytest.raises(ValueError, match=name):
        load_config(data_dir=tmp_path / "data")


def test_without_a_keys_file_setting_the_keys_stay_in_the_data_folder(clean_env, tmp_path):
    cfg = load_config(data_dir=tmp_path / "data")
    assert cfg.secrets_path == tmp_path / "data" / "secrets.json"
    assert cfg.secrets_path.exists()


def test_keys_in_the_data_folder_move_to_their_own_place_and_still_decrypt(clean_env, tmp_path):
    data, keys = tmp_path / "data", tmp_path / "keys" / "secrets.json"
    before = load_config(data_dir=data)
    token = Vault(before.encryption_keys).encrypt("certificate key")
    original = (data / "secrets.json").read_text(encoding="utf-8")

    clean_env.setenv("OFFICESENTRY_SECRETS_FILE", str(keys))
    after = load_config(data_dir=data)

    assert after.secrets_path == keys
    assert not (data / "secrets.json").exists()  # the data volume no longer holds the key
    assert keys.read_text(encoding="utf-8") == original
    assert stat.S_IMODE(keys.stat().st_mode) == 0o600
    assert Vault(after.encryption_keys).decrypt(token) == "certificate key"
    assert after.secret_key == before.secret_key  # nobody is signed out
    # The next start finds it where it now lives.
    assert load_config(data_dir=data).encryption_keys == before.encryption_keys


def test_a_move_cut_short_is_finished_on_the_next_start(clean_env, tmp_path):
    data, keys = tmp_path / "data", tmp_path / "keys" / "secrets.json"
    load_config(data_dir=data)
    keys.parent.mkdir()
    keys.write_bytes((data / "secrets.json").read_bytes())  # copied, but the old one not yet deleted
    clean_env.setenv("OFFICESENTRY_SECRETS_FILE", str(keys))
    load_config(data_dir=data)
    assert not (data / "secrets.json").exists() and keys.exists()


def test_two_different_key_files_stop_the_start(clean_env, tmp_path):
    data, keys = tmp_path / "data", tmp_path / "keys" / "secrets.json"
    load_config(data_dir=data)
    clean_env.setenv("OFFICESENTRY_SECRETS_FILE", str(keys))
    keys.parent.mkdir()
    keys.write_text(json.dumps({"secret_key": "other", "encryption_keys": ["other"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="two different key files"):
        load_config(data_dir=data)
    assert (data / "secrets.json").exists()  # nothing is deleted while it's unclear which is right


def test_a_keys_folder_it_cannot_write_says_so(clean_env, tmp_path):
    blocked = tmp_path / "file-not-folder"
    blocked.write_text("", encoding="utf-8")
    clean_env.setenv("OFFICESENTRY_SECRETS_FILE", str(blocked / "secrets.json"))
    with pytest.raises(ValueError, match="officesentry-keys volume"):
        load_config(data_dir=tmp_path / "data")


def test_keys_from_environment_variables_leave_any_file_alone(clean_env, tmp_path):
    data = tmp_path / "data"
    load_config(data_dir=data)
    clean_env.setenv("OFFICESENTRY_SECRET_KEY", "s" * 40)
    clean_env.setenv("OFFICESENTRY_ENCRYPTION_KEYS", "k")
    clean_env.setenv("OFFICESENTRY_SECRETS_FILE", str(tmp_path / "keys" / "secrets.json"))
    load_config(data_dir=data)
    assert (data / "secrets.json").exists() and not (tmp_path / "keys").exists()


def test_compose_keeps_the_keys_out_of_the_data_volume():
    compose = yaml.safe_load((V2 / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    for name in ("web", "worker"):
        assert "officesentry-keys:/keys" in services[name]["volumes"]
        assert services[name]["environment"]["OFFICESENTRY_SECRETS_FILE"] == "/keys/secrets.json"
    # The public demo's made-up keys stay in its own memory; the off-site copy reads the data volume only.
    demo = yaml.safe_load((V2 / "deploy" / "public" / "compose.yml").read_text(encoding="utf-8"))["services"]["demo"]
    assert "volumes" not in demo and "OFFICESENTRY_SECRETS_FILE" not in demo["environment"]
    assert services["offsite"]["volumes"] == ["officesentry-data:/data:ro", "restic-cache:/cache"]
    assert services["offsite"]["profiles"] == ["offsite"]
    assert re.fullmatch(r"restic/restic:[0-9.]+@sha256:[0-9a-f]{64}", services["offsite"]["image"])
    dockerfile = (V2 / "Dockerfile").read_text(encoding="utf-8")
    assert "chown sentry /data /keys" in dockerfile  # a new keys volume is the app user's


def test_a_slow_upgrade_is_given_time_before_the_deploy_gives_up():
    """A database upgrade copies the whole database before the portal answers, which takes minutes on a large
    one: the health check's start period, the start script's wait and Docker's live-restore allow for it."""
    compose = yaml.safe_load((V2 / "docker-compose.yml").read_text(encoding="utf-8"))
    assert compose["services"]["web"]["healthcheck"]["start_period"] == "15m"
    assert compose["services"]["worker"]["depends_on"]["web"] == {"condition": "service_healthy"}
    start = (V2 / "deploy" / "start.sh").read_text(encoding="utf-8")
    assert "seq 200" in start  # 200 looks, 5 seconds apart
    cloud_init = yaml.safe_load((V2 / "deploy" / "cloud-init.yaml").read_text(encoding="utf-8"))
    daemon = next(f for f in cloud_init["write_files"] if f["path"] == "/etc/docker/daemon.json")
    assert json.loads(daemon["content"])["live-restore"] is True  # a Docker update doesn't stop the portal


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_start_script_starts_the_worker_once_the_portal_is_healthy(tmp_path):
    """When "compose up" gives up waiting for the portal (an upgrade slower than its health check allows), the
    worker is created but not started: the script waits for the portal itself, then brings the rest up."""
    folder = tmp_path / "v2"
    (folder / "deploy").mkdir(parents=True)
    (folder / ".env").write_text("COMPOSE_PROFILES=\n", encoding="utf-8")
    shutil.copy(V2 / "deploy" / "start.sh", folder / "deploy" / "start.sh")
    shutil.copy(V2 / "deploy" / "jobs.sh", folder / "deploy" / "jobs.sh")
    tools = tmp_path / "bin"
    tools.mkdir()
    calls = tmp_path / "calls.log"
    docker = tools / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        "args = sys.argv[1:]\n"
        f"log = pathlib.Path({str(calls)!r})\n"
        "log.open('a').write(' '.join(args) + '\\n')\n"
        "if args == ['compose', 'up', '-d', '--remove-orphans']:\n"
        "    if 'compose up -d --remove-orphans' not in log.read_text().splitlines()[:-1]:\n"
        "        print('dependency failed to start: container v2-web-1 is unhealthy', file=sys.stderr)\n"
        "        sys.exit(1)\n"
        "elif args[:2] == ['compose', 'ps'] and '-q' in args:\n"
        "    print('cid-web' if args[-1] == 'web' else '', end='')\n"
        "elif args[0] == 'inspect':\n"
        "    print('healthy')\n",
        encoding="utf-8",
    )
    (tools / "crontab").write_text("#!/usr/bin/env bash\ncat > /dev/null\n", encoding="utf-8")
    for tool in (docker, tools / "crontab"):
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    env = dict(os.environ, OFFICESENTRY_DIR=str(folder), PATH=f"{tools}{os.pathsep}{os.environ['PATH']}")
    out = subprocess.run(  # noqa: S603
        [shutil.which("bash"), "deploy/start.sh"], cwd=folder, env=env, capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "The portal is healthy now; starting the rest." in out.stdout
    lines = calls.read_text(encoding="utf-8").splitlines()
    first = lines.index("compose up -d --remove-orphans")
    second = lines.index("compose up -d --remove-orphans", first + 1)  # once the portal was seen healthy
    assert lines.count("compose up -d --remove-orphans") == 2
    assert first < lines.index("inspect -f {{.State.Health.Status}} cid-web") < second


def test_offsite_and_drill_record_their_status(env_cfg):
    main(["backup-status", "--record", "offsite", "--ok", "--detail", "copy 1a2b3c4d"])
    main(["backup-status", "--record", "offsite", "--failed", "--error", "wrong password or no key found"])
    entry = backups.read_status(env_cfg.data_dir)["offsite"]
    assert entry["ok"] is False and entry["error"] == "wrong password or no key found"
    assert entry["last_success"] and entry["last_attempt"] >= entry["last_success"]
    with pytest.raises(SystemExit):
        main(["backup-status", "--record", "offsite"])  # neither --ok nor --failed


def test_backup_status_reads_plainly(env_cfg, db, capsys):
    main(["backup"])
    main(["backup-status", "--record", "offsite", "--failed", "--error", "couldn't reach the off-site storage"])
    capsys.readouterr()
    main(["backup-status"])
    out = capsys.readouterr().out
    assert re.search(r"^Database copy: OK at .*officesentry-\d{8}-\d{6}\.db\.gz", out, re.M)
    assert "Off-site copy: FAILED" in out and "last worked never" in out and "off-site storage" in out
    main(["backup-status", "--json"])
    assert set(json.loads(capsys.readouterr().out)) == {"local", "offsite"}


def test_check_backup_opens_a_backup_without_restoring_it(env_cfg, db, two_tenants, capsys):
    main(["backup"])
    saved = next((env_cfg.data_dir / "backups").glob("officesentry-*.db.gz"))
    capsys.readouterr()
    main(["check-backup", str(saved), "--record"])
    assert "2 clients" in capsys.readouterr().out
    entry = backups.read_status(env_cfg.data_dir)["restore_check"]
    assert entry["ok"] and entry["detail"].startswith(saved.name) and "2 clients" in entry["detail"]
    assert not list(saved.parent.glob("restore-*.part"))  # the unpacked copy is cleaned up


def test_check_backup_records_a_damaged_backup(env_cfg, db, two_tenants):
    main(["backup"])
    saved = next((env_cfg.data_dir / "backups").glob("officesentry-*.db.gz"))
    saved.write_bytes(saved.read_bytes()[:200])  # cut short, as a broken upload would be
    with pytest.raises(SystemExit, match="Check failed"):
        main(["check-backup", str(saved), "--record"])
    entry = backups.read_status(env_cfg.data_dir)["restore_check"]
    assert entry["ok"] is False and saved.name in entry["error"]


def test_production_from_releases_is_off_unless_set():
    workflow = (V2.parent / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    assert "RELEASES_ONLY: ${{ vars.PRODUCTION_FROM_TAGS == 'true' }}" in workflow
    # Production only ever runs a release tag's published image, pinned by digest.
    assert 'echo "OFFICESENTRY_VERSION=${REF#v}@$RELEASE_DIGEST"' in workflow
    # The off-site password reaches the server's .env.backup only, never the portal's .env.
    env_block = workflow[workflow.index("} > v2/.env\n") - 2000 : workflow.index("} > v2/.env\n")]
    assert "BACKUP_PASSWORD" not in env_block.split("{\n", 1)[-1]


def test_deploy_keeps_the_settings_files_out_of_the_demo_and_closes_ssh_after_a_lost_runner():
    workflow = (V2.parent / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    # The demo's copy of the code never gets .env.backup (the off-site password and storage keys) or .env;
    # rsync's --delete leaves excluded files alone, so copies from before are removed by name.
    demo = workflow[workflow.index("name: Build the public demo") : workflow.index("name: Pull the release image")]
    assert "--exclude '.env*'" in demo and "--exclude .env " not in demo
    assert "rm -f /opt/officesentry/demo-src/.env /opt/officesentry/demo-src/.env.*" in demo
    # The firewall rule is removed by the deploy job's last step however it ends, and by a job of its own
    # afterwards for the one case that step can't cover: a runner that disappears.
    jobs = yaml.safe_load(workflow)["jobs"]
    assert jobs["close-ssh"]["needs"] == "server" and jobs["close-ssh"]["if"].startswith("always()")
    assert jobs["close-ssh"]["env"]["RULE"] == jobs["server"]["env"]["RULE"]
    close = jobs["close-ssh"]["steps"][0]["run"]
    assert "select(.description != $rule)" in close and "/actions/set_rules" in close
    assert workflow.count("cancel-in-progress: false") == 1  # runs never overlap, so the rule found is this run's


@pytest.fixture
def env_cfg(cfg, monkeypatch):
    monkeypatch.setenv("OFFICESENTRY_DATA_DIR", str(cfg.data_dir))
    monkeypatch.delenv("OFFICESENTRY_SECRETS_FILE", raising=False)
    return cfg
