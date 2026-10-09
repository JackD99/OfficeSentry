"""The GitHub release notes for a tag, from CHANGELOG.md, after checking the tag matches the code.

    python scripts/release_notes.py v2.1.0 > notes.md

The release workflow (.github/workflows/release.yml) runs this before it builds anything. It fails when:
- the tag isn't a version (``v2.1.0``, ``v2.1.0-rc.1``) or differs from ``officesentry.__version__``
  (``2.1.0``, ``2.1.0rc1``), so the image never reports a version other than its tag. A candidate may also
  come from the development version before its release (``2.1.0.dev0``); the workflow then writes the
  candidate's version into the image;
- CHANGELOG.md has no ``## 2.1.0`` section, or a final release's section still says "unreleased".

A pre-release (``-rc.1``) uses the section of the release it leads up to, so the notes can be drafted
before the final tag. It writes ``prerelease=true|false`` and ``python_version=`` (``2.1.0rc1``) to
$GITHUB_OUTPUT when that is set.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

V2 = Path(__file__).resolve().parent.parent
CHANGELOG = V2.parent / "CHANGELOG.md"


class ReleaseError(Exception):
    pass


def code_version(init: Path = V2 / "officesentry" / "__init__.py") -> str:
    match = re.search(r'^__version__ = "([^"]+)"', init.read_text(encoding="utf-8"), re.M)
    if not match:
        raise ReleaseError(f"No __version__ in {init}.")
    return match[1]


PRE = {"alpha": "a", "beta": "b", "rc": "rc"}


def parse_tag(tag: str) -> tuple[str, str]:
    """``v2.1.0-rc.1`` -> (``2.1.0``, ``2.1.0rc1``): the release it leads to, and the same version as Python
    writes it (PEP 440), which is what ``__version__`` holds."""
    match = re.fullmatch(r"v(\d+\.\d+\.\d+)(?:-(alpha|beta|rc)\.(\d+))?", tag)
    if not match:
        raise ReleaseError(f"{tag} isn't a release tag: use v<major>.<minor>.<patch>, such as v2.1.0, or "
                           "v2.1.0-rc.1 for a release candidate.")
    return match[1], match[1] + (f"{PRE[match[2]]}{match[3]}" if match[2] else "")


def section(changelog: str, release: str, prerelease: bool) -> str:
    """The text under ``## <release>`` (up to the next ``## ``), without the heading."""
    lines = changelog.splitlines()
    for i, line in enumerate(lines):
        heading = re.match(r"## \[?(\d+\.\d+\.\d+)\]?(.*)", line)
        if not heading or heading[1] != release:
            continue
        if not prerelease and "unreleased" in heading[2].lower():
            raise ReleaseError(f"CHANGELOG.md still calls {release} unreleased: put the release date in its "
                               "heading before tagging it.")
        end = next((j for j in range(i + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
        body = "\n".join(lines[i + 1:end]).strip()
        if not body:
            raise ReleaseError(f"CHANGELOG.md's {release} section is empty.")
        return body + "\n"
    raise ReleaseError(f"CHANGELOG.md has no '## {release}' section: add one before tagging.")


def notes(tag: str, changelog: str, version_in_code: str) -> tuple[str, bool]:
    release, python_version = parse_tag(tag)
    prerelease = python_version != release
    # A candidate can be cut from the development version leading to it (2.1.0.dev0 for v2.1.0-rc.1): the
    # workflow stamps the candidate's version into the image, so trying the pipeline needs no version PR.
    candidate_of_dev = prerelease and re.fullmatch(rf"{re.escape(release)}\.dev\d+", version_in_code)
    if python_version != version_in_code and not candidate_of_dev:
        raise ReleaseError(f"The tag {tag} needs the code's version (officesentry/__init__.py) to be "
                           f"{python_version}, but it is {version_in_code}.")
    return section(changelog, release, prerelease), prerelease


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        text, prerelease = notes(argv[0], CHANGELOG.read_text(encoding="utf-8"), code_version())
    except ReleaseError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    sys.stdout.write(text)
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as fh:
            fh.write(f"prerelease={str(prerelease).lower()}\npython_version={parse_tag(argv[0])[1]}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
