"""``deploy/jobs.sh restore``, the off-site disaster recovery (DEPLOY.md, "Restoring from the off-site copy"),
run for real against a stand-in ``docker`` that plays restic and runs ``python -m officesentry restore`` here."""

import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from officesentry.__main__ import main
from officesentry.db import connect

V2 = Path(__file__).resolve().parent.parent
NAME = "officesentry-20261009-034500.db.gz"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or not shutil.which("bash") or not shutil.which("flock"), reason="the server jobs run on Linux"
)

# Plays `docker`: restic's commands in the offsite service, and the web service's restore, which runs the real
# command against the test's data folder. /restore in the container is <root>/restore here. Logs every call.
FAKE_DOCKER = """\
#!{python}
import os, shutil, subprocess, sys
from pathlib import Path

root = Path(os.environ["FAKE_ROOT"])
args = sys.argv[1:]
with open(root / "docker.log", "a", encoding="utf-8") as log:
    log.write(" ".join(args) + "\\n")
if args[:3] == ["compose", "--profile", "offsite"]:
    rest = args[args.index("offsite", 3) + 1:]
    if "ls" in rest:
        print("/data/backups/{name}")
    elif "restore" in rest:
        target = root / "restore" / "data" / "backups"
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy(os.environ["FAKE_BACKUP"], target / "{name}")
        if "/data/backups/events" in rest and os.environ.get("FAKE_EVENTS"):
            shutil.copytree(os.environ["FAKE_EVENTS"], target / "events")
    sys.exit(0)
if args[:2] == ["compose", "run"] and "officesentry" in args and "restore" in args:
    file = args[-1]
    assert file.startswith("/restore/")
    sys.exit(subprocess.run([sys.executable, "-m", "officesentry", "restore", str(root) + file]).returncode)
sys.exit(0)
"""


@pytest.fixture
def server(cfg, monkeypatch, tmp_path):
    """An app folder with off-site backups set up, and the stand-in docker first on PATH."""
    monkeypatch.setenv("OFFICESENTRY_DATA_DIR", str(cfg.data_dir))
    app = tmp_path / "app" / "v2"
    app.mkdir(parents=True)
    (app / ".env.backup").write_text("RESTIC_REPOSITORY='s3:https://storage.example/officesentry'\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER.format(python=sys.executable, name=NAME), encoding="utf-8")
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("OFFICESENTRY_DIR", str(app))
    monkeypatch.setenv("FAKE_ROOT", str(tmp_path))
    monkeypatch.delenv("JOBS_LOG", raising=False)
    return tmp_path


def _restore_job(root):
    done = subprocess.run(  # noqa: S603 (the repository's own script)
        [shutil.which("bash"), str(V2 / "deploy" / "jobs.sh"), "restore"], capture_output=True, text=True, timeout=120
    )
    calls = (root / "docker.log").read_text(encoding="utf-8").splitlines()
    return done, calls


def _tenants(path):
    conn = connect(path)
    try:
        return [r["name"] for r in conn.execute("SELECT name FROM tenants ORDER BY id")]
    finally:
        conn.close()


def test_the_offsite_restore_replaces_a_damaged_database_and_starts_again(cfg, db, two_tenants, server, monkeypatch):
    main(["backup"])
    monkeypatch.setenv("FAKE_BACKUP", str(next((cfg.data_dir / "backups").glob("officesentry-*.db.gz"))))
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()
    with open(cfg.db_path, "r+b") as f:
        f.write(b"\0" * 100)  # the damage that sent someone here
    check = sqlite3.connect(cfg.db_path)
    with pytest.raises(sqlite3.DatabaseError):
        check.execute("SELECT 1 FROM tenants")
    check.close()

    done, calls = _restore_job(server)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "was damaged" in done.stdout + done.stderr
    assert _tenants(cfg.db_path) == ["Alpha Ltd", "Bravo Inc"]
    assert list((cfg.data_dir / "backups").glob("pre-restore-*-damaged.db"))
    assert calls.index("compose stop web worker") < calls.index("compose up -d")


def test_a_failed_offsite_restore_leaves_the_portal_stopped(cfg, db, two_tenants, server, monkeypatch, tmp_path):
    broken = tmp_path / NAME
    broken.write_bytes(b"\x1f\x8b" + b"cut short")
    monkeypatch.setenv("FAKE_BACKUP", str(broken))

    done, calls = _restore_job(server)
    assert done.returncode == 1
    assert "Not restored" in done.stderr
    assert "left stopped" in done.stdout and "docker compose up -d" in done.stdout
    assert "compose stop web worker" in calls
    assert "compose up -d" not in calls  # nothing starts on what's left
    assert _tenants(cfg.db_path) == ["Alpha Ltd", "Bravo Inc"]


def test_the_offsite_restore_brings_back_the_activity_history(cfg, db, two_tenants, server, monkeypatch):
    month = cfg.data_dir / "events" / "signins" / "2026-09.db"
    month.parent.mkdir(parents=True)
    with closing(sqlite3.connect(month)) as conn:
        conn.execute("CREATE TABLE signin (id TEXT)")
        conn.execute("INSERT INTO signin VALUES ('one sign-in')")
        conn.commit()
    main(["backup"])
    copies = cfg.data_dir / "backups"
    monkeypatch.setenv("FAKE_BACKUP", str(next(copies.glob("officesentry-*.db.gz"))))
    monkeypatch.setenv("FAKE_EVENTS", str(copies / "events"))
    shutil.rmtree(cfg.data_dir / "events")  # a new server: no history yet

    done, calls = _restore_job(server)
    assert done.returncode == 0, done.stdout + done.stderr
    assert any("--include /data/backups/events" in c for c in calls)
    assert "Put back 1 missing activity history file" in done.stdout
    with closing(sqlite3.connect(month)) as conn:
        assert conn.execute("SELECT id FROM signin").fetchall() == [("one sign-in",)]
