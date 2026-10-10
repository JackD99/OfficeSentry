"""Every release's database upgrades to this code, and every page then opens.

For each release tag (``v2*``), that release's own ``scripts/demo_data.py`` builds a demo database with the
schema and data that release wrote, from the tag's code (``git archive``, so the working tree is untouched).
This code then upgrades it, checks SQLite's integrity, signs in and opens every page a crawl can reach from
the dashboard and each tenant's overview. A server error or crash on any page fails the test, as would an
upgrade that refuses or leaves the schema behind.

``NIGHTLY_RELEASES`` (``v2.0.0 v2.1.0``) limits the tags; otherwise every ``v2*`` tag in the repository is
used, so a shallow CI checkout must fetch the tags first (``nightly.yml`` does).
"""

from __future__ import annotations

import os
import subprocess  # noqa: S404 (git and the release's own demo script, on files this test made)
import sys
from pathlib import Path

import pytest

from .conftest import V2, crawl, portal

ROOT = V2.parent
PASSWORD = "nightly upgrade check"  # noqa: S105 (a throwaway demo database)


def _tags() -> list[str]:
    if os.environ.get("NIGHTLY_RELEASES"):
        return os.environ["NIGHTLY_RELEASES"].split()
    out = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-c",
            "versionsort.suffix=-",
            "tag",
            "--list",
            "v2*",
            "--sort=v:refname",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return out.split()


def _release_database(tag: str, tmp: Path) -> Path:
    """A demo data folder written by ``tag``'s code (its database and secrets.json)."""
    src = tmp / "src"
    src.mkdir()
    archive = subprocess.run(  # noqa: S603
        ["git", "archive", "--format=tar", tag, "v2"],  # noqa: S607
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(src)], input=archive, check=True)  # noqa: S603, S607
    data = tmp / "data"
    env = {k: v for k, v in os.environ.items() if not k.startswith("OFFICESENTRY_")}
    env["PYTHONPATH"] = str(src / "v2")  # the release's own officesentry package, not this one
    done = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "scripts/demo_data.py",
            "--data-dir",
            str(data),
            "--password",
            PASSWORD,
        ],
        cwd=src / "v2",
        env=env,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, f"{tag}'s demo_data.py failed:\n{done.stdout[-2000:]}\n{done.stderr[-4000:]}"
    return data


@pytest.mark.parametrize("tag", _tags())
def test_release_database_upgrades_and_every_page_opens(tag, tmp_path, summary):
    from officesentry.db import connect, migrate

    data = _release_database(tag, tmp_path)
    db = connect(data / "officesentry.db")
    before = db.execute("PRAGMA user_version").fetchone()[0]
    migrate(db)
    after = db.execute("PRAGMA user_version").fetchone()[0]
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    tenants = [r[0] for r in db.execute("SELECT id FROM tenants ORDER BY id")]
    db.close()
    assert tenants, f"{tag}'s demo database has no tenants"

    _, client = portal(data, "demo", PASSWORD)
    opened, problems = crawl(client, ["/", *(f"/tenants/{t}" for t in tenants)])
    summary.append(f"- Upgrade from {tag}: schema {before} to {after}, {opened} pages opened, {len(problems)} problems")
    assert opened > 100, f"only {opened} pages opened: is the crawl signed in?"
    assert not problems, f"after upgrading {tag}'s database:\n" + "\n".join(problems[:40])
