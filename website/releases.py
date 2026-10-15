"""What the website shows of the docs: only what's released.

The docs follow the code on main, so a pull request that adds a feature describes it there straight away, but
the site shows what people can install (website/README.md, "Not released yet"). A block of lines from
"<!-- since 2.4.0 -->" to "<!-- /since -->", or one line carrying "<!-- since 2.4.0 -->" (a table row, say),
stays off the site until CHANGELOG.md gives 2.4.0 a date. GitHub hides the comments and shows it all, as it
shows main. Standard library only, so v2's tests can check it without the website's own packages
(v2/tests/test_website_releases.py)."""

from __future__ import annotations

import re
from dataclasses import dataclass

SINCE = re.compile(r"<!--\s*since\s+v?(\d+\.\d+\.\d+)\s*-->")
SINCE_END = re.compile(r"\s*<!--\s*/since\s*-->\s*")
HEADING = re.compile(r"^## \[?v?(\d+\.\d+\.\d+)\]? - (.+)$", re.M)


class MarkerError(ValueError):
    """A <!-- since --> marker the site can't follow, with the file and line it's on."""


@dataclass
class Releases:
    """CHANGELOG.md's versions, newest first, and which of them the site shows."""

    dates: dict  # version: what its heading says after " - ", a date or "Unreleased"
    preview: bool = False  # SITE_PREVIEW: show the coming release too

    @classmethod
    def parse(cls, changelog: str, preview: bool = False) -> Releases:
        return cls({v: when.strip() for v, when in HEADING.findall(changelog)}, preview)

    def dated(self, version: str) -> bool:
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.dates.get(version, "")))

    def shown(self, version: str) -> bool:
        return self.preview or self.dated(version)

    def newest(self) -> str:
        """The newest released version, or "" before the first release."""
        return next((v for v in self.dates if self.dated(v)), "")


def released_only(text: str, path: str, releases: Releases) -> str:
    """A doc without the blocks and lines of a release the site doesn't show yet."""
    out, opened, hiding = [], 0, False
    for n, line in enumerate(text.splitlines(), 1):
        mark = SINCE.search(line)
        if mark and mark[1] not in releases.dates:
            raise MarkerError(
                f"{path}:{n}: <!-- since {mark[1]} --> names a version with no '## [{mark[1]}] - ...' heading "
                "in CHANGELOG.md"
            )
        if mark and line.strip() == mark[0]:
            if opened:
                raise MarkerError(f"{path}:{n}: <!-- since --> inside the one on line {opened}; close that first")
            opened, hiding = n, not releases.shown(mark[1])
        elif SINCE_END.fullmatch(line):
            if not opened:
                raise MarkerError(f"{path}:{n}: <!-- /since --> without a <!-- since ... --> before it")
            opened, hiding = 0, False
        elif hiding or (mark and not releases.shown(mark[1])):
            continue
        else:
            out.append(SINCE.sub("", line).rstrip() if mark else line)
    if opened:
        raise MarkerError(f"{path}:{opened}: <!-- since --> is never closed with <!-- /since -->")
    return "\n".join(out) + "\n"


def released_changes(changelog: str, releases: Releases) -> str:
    """CHANGELOG.md without the sections of releases the site doesn't show yet (the "Unreleased" one)."""
    out, hiding = [], False
    for line in changelog.splitlines():
        if line.startswith("## "):
            heading = HEADING.match(line)
            hiding = bool(heading) and not releases.shown(heading[1])
        if not hiding:
            out.append(line)
    return "\n".join(out) + "\n"
