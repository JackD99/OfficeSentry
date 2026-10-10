"""Releases: the version people see, the lock files, pinned images and the release notes script."""

import importlib.util
import re
import tomllib
from pathlib import Path

import pytest

from officesentry import __version__, backups
from officesentry import db as db_module
from officesentry.db import connect, migrate, stored_app_version

from .conftest import login, make_user

V2 = Path(__file__).resolve().parent.parent


def _release_notes():
    spec = importlib.util.spec_from_file_location("release_notes", V2 / "scripts" / "release_notes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pins(name):
    """``{package: version}`` from a pip-compile lock file."""
    text = (V2 / name).read_text(encoding="utf-8")
    return {m[1].lower(): m[2] for m in re.finditer(r"^([A-Za-z0-9_.-]+)==(\S+)", text, re.M)}


def test_health_and_the_menu_say_which_version_is_running(client, db):
    assert client.get("/health").json == {"ok": True, "version": __version__}
    # Strangers on the sign-in page don't see it.
    assert __version__ not in client.get("/login").get_data(as_text=True)
    make_user(db, "ann")
    login(client, "ann")
    page = client.get("/").get_data(as_text=True)
    assert f"Office Sentry {__version__}</p>" in page


def test_the_database_remembers_the_release_and_pre_upgrade_copies_name_it(cfg, monkeypatch):
    every = db_module.migration_files()
    monkeypatch.setattr(db_module, "migration_files", lambda: [m for m in every if m[0] <= 4])
    monkeypatch.setattr("officesentry.__version__", "2.0.0")
    conn = connect(cfg.db_path)
    migrate(conn)
    assert stored_app_version(conn) == "2.0.0"

    monkeypatch.setattr(db_module, "migration_files", lambda: every)
    monkeypatch.setattr("officesentry.__version__", "2.1.0")
    migrate(conn)
    assert stored_app_version(conn) == "2.1.0"
    [saved] = (cfg.data_dir / "backups").glob("pre-upgrade-*.db")
    assert re.fullmatch(rf"pre-upgrade-v4-to-v{every[-1][0]}-\d{{8}}-\d{{6}}-from-2\.0\.0\.db", saved.name)
    # The copy is the database as 2.0.0 left it, so restoring it means going back to 2.0.0.
    assert stored_app_version(connect(saved)) == "2.0.0"

    # Going back to 2.0.0's code without restoring: the refusal names the release that upgraded it,
    # and the copy to restore.
    monkeypatch.setattr(db_module, "migration_files", lambda: [m for m in every if m[0] <= 4])
    with pytest.raises(db_module.DatabaseTooNew, match=r"upgraded by Office Sentry 2\.1\.0 .*" + re.escape(saved.name)):
        migrate(conn)


def test_a_database_from_before_releases_gets_a_plain_pre_upgrade_name(cfg, monkeypatch):
    every = db_module.migration_files()
    monkeypatch.setattr(db_module, "migration_files", lambda: [m for m in every if m[0] <= 4])
    conn = connect(cfg.db_path)
    migrate(conn)
    conn.execute("DELETE FROM settings WHERE key = ?", (db_module.APP_VERSION_KEY,))
    monkeypatch.setattr(db_module, "migration_files", lambda: every)
    migrate(conn)
    [saved] = (cfg.data_dir / "backups").glob("pre-upgrade-*.db")
    assert re.fullmatch(rf"pre-upgrade-v4-to-v{every[-1][0]}-\d{{8}}-\d{{6}}\.db", saved.name)


def test_rotation_keeps_the_newest_copies_whatever_their_database_version(tmp_path):
    names = [
        "pre-upgrade-v8-to-v9-20260101-000000.db",
        "pre-upgrade-v9-to-v10-20260201-000000.db",
        "pre-upgrade-v10-to-v11-20260301-000000.db",
        "pre-upgrade-v11-to-v12-20260401-000000-from-2.0.0.db",
    ]
    for name in names:
        (tmp_path / name).write_bytes(b"")
    deleted = backups.rotate(tmp_path, backups.PRE_UPGRADE_PREFIX, 2)
    assert sorted(p.name for p in deleted) == sorted(names[:2])
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(names[2:])


def test_the_lock_files_pin_every_dependency_with_hashes():
    image, dev = _pins("requirements.txt"), _pins("requirements-dev.txt")
    project = tomllib.loads((V2 / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    for requirement in project["dependencies"] + project["optional-dependencies"]["pdf"]:
        if "win32'" in requirement and "!=" not in requirement:
            continue  # Windows only; the image and CI are Linux
        name = re.match(r"[A-Za-z0-9_.-]+", requirement)[0].lower()
        assert name in image, f"{name} is missing from requirements.txt: refresh the lock (CONTRIBUTING.md)"
    # CI tests the versions the image ships.
    assert {k: v for k, v in dev.items() if k in image} == image
    for name in ("requirements.txt", "requirements-dev.txt"):
        text = (V2 / name).read_text(encoding="utf-8")
        assert all(
            "--hash=sha256:" in block
            for block in re.split(r"\n(?=[A-Za-z])", text.split("\n\n", 1)[-1])
            if "==" in block
        ), f"every pin in {name} needs its hashes"


def test_images_are_pinned():
    dockerfile = (V2 / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^FROM public\.ecr\.aws/docker/library/python:3\.12-slim@sha256:[0-9a-f]{64}$", dockerfile, re.M)
    compose = (V2 / "docker-compose.yml").read_text(encoding="utf-8")
    assert "image: ghcr.io/jackd99/officesentry:${OFFICESENTRY_VERSION:-2}" in compose
    assert "build:" not in compose  # building is docker-compose.build.yml's job
    assert re.search(r"image: caddy:2@sha256:[0-9a-f]{64}$", compose, re.M)
    assert "build: ." in (V2 / "docker-compose.build.yml").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("tag", "code", "release", "prerelease"),
    [
        ("v2.1.0", "2.1.0", "2.1.0", False),
        ("v2.1.0-rc.1", "2.1.0rc1", "2.1.0", True),
        ("v2.1.0-beta.2", "2.1.0b2", "2.1.0", True),
    ],
)
def test_release_notes_come_from_the_changelog(tag, code, release, prerelease):
    notes = _release_notes()
    changelog = (
        f"# Changelog\n\n## {release} (2026-11-02)\n\n### Added\n\n- Things.\n\n## 2.0.0 (2026-10-10)\n\n- Old.\n"
    )
    assert notes.notes(tag, changelog, code) == ("### Added\n\n- Things.\n", prerelease)


def test_release_notes_refuse_a_tag_that_doesnt_fit():
    notes = _release_notes()
    changelog = "## 2.1.0 (unreleased)\n\n- Things.\n"
    with pytest.raises(notes.ReleaseError, match="needs the code's version .* to be 2.1.0, but it is 2.0.0"):
        notes.notes("v2.1.0", changelog, "2.0.0")
    with pytest.raises(notes.ReleaseError, match="isn't a release tag"):
        notes.notes("v2.1", changelog, "2.1")
    with pytest.raises(notes.ReleaseError, match="still calls 2.1.0 unreleased"):
        notes.notes("v2.1.0", changelog, "2.1.0")
    # A release candidate may come before the date is known, and from the development version.
    assert notes.notes("v2.1.0-rc.1", changelog, "2.1.0rc1")[0] == "- Things.\n"
    assert notes.notes("v2.1.0-rc.1", changelog, "2.1.0.dev0") == ("- Things.\n", True)
    with pytest.raises(notes.ReleaseError, match="needs the code's version"):
        notes.notes("v2.1.0-rc.1", changelog, "2.0.0.dev0")
    with pytest.raises(notes.ReleaseError, match="needs the code's version"):
        notes.notes("v2.1.0", "## 2.1.0 (2026-11-02)\n\n- Things.\n", "2.1.0.dev0")
    with pytest.raises(notes.ReleaseError, match="no '## 2.2.0' section"):
        notes.notes("v2.2.0", changelog, "2.2.0")


def test_the_changelog_has_a_section_for_the_code_version():
    notes = _release_notes()
    version = notes.code_version()
    assert version == __version__
    release = re.match(r"\d+\.\d+\.\d+", version)[0]
    assert notes.section(notes.CHANGELOG.read_text(encoding="utf-8"), release, prerelease=True)


def test_old_images_are_pruned_with_their_parts():
    spec = importlib.util.spec_from_file_location("prune_images", V2 / "scripts" / "prune_images.py")
    prune = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prune)

    def version(vid, when, *tags):
        return {
            "id": vid,
            "name": f"sha256:{vid}",
            "created_at": f"2026-{when}Z",
            "metadata": {"container": {"tags": list(tags)}},
        }

    versions = [
        version(1, "10-01T10:00:00", "2.0.0-rc.1"),
        version(2, "10-01T09:58:00"),  # its platform image
        version(3, "10-01T10:05:00", "sha256-1"),  # its signed attestation
        version(4, "10-15T10:00:00", "2.0.0", "2.0", "2", "latest"),
        version(5, "10-15T09:59:00"),
        version(6, "10-20T10:00:00"),  # a push whose scan failed: never tagged
        version(7, "11-02T10:00:00", "2.1.0"),
        version(8, "11-02T09:59:00"),
        version(9, "12-01T10:00:00", "2.2.0"),
        version(10, "12-01T09:59:00"),
    ]
    assert prune.to_delete(versions, keep=4) == []
    assert [v["id"] for v in prune.to_delete(versions, keep=3)] == [1, 2, 3]
    assert [v["id"] for v in prune.to_delete(versions, keep=1)] == [1, 2, 3, 4, 5, 6, 7, 8]
