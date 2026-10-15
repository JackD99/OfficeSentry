"""The website shows only what's released (website/releases.py): docs on main can describe the next release,
marked <!-- since x.y.z -->, without the site showing it before CHANGELOG.md dates that version."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = """# Changelog

## [2.4.0] - Unreleased

### Added

- The audit log add-on.

## [2.3.1] - 2026-10-12

### Fixed

- Site access.
"""
DOC = """# A guide

Released words.

<!-- since 2.4.0 -->
## The add-on

What it does.
<!-- /since -->

| Check | Area |
|---|---|
| MFA for admins | Admins |
| The audit log is on <!-- since 2.4.0 --> | Email |
| Old one <!-- since 2.3.1 --> | Email |
"""


def _module():
    spec = importlib.util.spec_from_file_location("website_releases", ROOT / "website" / "releases.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up there
    spec.loader.exec_module(module)
    return module


def test_the_next_release_stays_off_the_site_until_it_has_a_date():
    m = _module()
    releases = m.Releases.parse(CHANGELOG)
    shown = m.released_only(DOC, "guide.md", releases)
    assert "Released words." in shown and "| MFA for admins | Admins |" in shown
    assert "add-on" not in shown and "What it does" not in shown and "audit log" not in shown
    assert "| Old one  | Email |" in shown and "<!--" not in shown  # released: shown, marker gone
    changes = m.released_changes(CHANGELOG, releases)
    assert "2.4.0" not in changes and "audit log" not in changes and "## [2.3.1] - 2026-10-12" in changes
    assert releases.newest() == "2.3.1"


def test_once_released_or_previewed_everything_shows():
    m = _module()
    for releases in (
        m.Releases.parse(CHANGELOG.replace("Unreleased", "2026-12-01")),
        m.Releases.parse(CHANGELOG, preview=True),
    ):
        shown = m.released_only(DOC, "guide.md", releases)
        assert "## The add-on" in shown and "What it does." in shown and "| The audit log is on  | Email |" in shown
        assert "<!--" not in shown
        assert "audit log add-on" in m.released_changes(CHANGELOG, releases)
    assert m.Releases.parse(CHANGELOG.replace("Unreleased", "2026-12-01")).newest() == "2.4.0"


def test_a_whole_page_can_wait_for_its_release():
    m = _module()
    page = "<!-- since 2.4.0 -->\n# The audit log add-on\n\nWords.\n<!-- /since -->\n"
    assert not m.released_only(page, "addon.md", m.Releases.parse(CHANGELOG)).strip()


@pytest.mark.parametrize(
    "text, problem",
    [
        ("<!-- since 2.5.0 -->\nWords\n<!-- /since -->\n", "no '## [2.5.0] - ...' heading"),
        ("<!-- since 2.4.0 -->\nWords\n", "never closed"),
        ("Words\n<!-- /since -->\n", "without a <!-- since"),
        ("<!-- since 2.4.0 -->\n<!-- since 2.4.0 -->\n<!-- /since -->\n", "inside the one on line 1"),
    ],
)
def test_a_marker_the_site_cant_follow_stops_the_build(text, problem):
    m = _module()
    with pytest.raises(m.MarkerError) as error:
        m.released_only(text, "doc.md", m.Releases.parse(CHANGELOG))
    assert str(error.value).startswith("doc.md:") and problem in str(error.value)


def test_every_doc_in_the_repository_follows_the_markers():
    """The same check the site's build makes, so a broken marker fails here too, not only in the Website check."""
    m = _module()
    releases = m.Releases.parse((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    skip = {"node_modules", ".git", ".venv", "_site"}
    for path in ROOT.rglob("*.md"):
        if not skip & set(path.relative_to(ROOT).parts):
            m.released_only(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)), releases)
