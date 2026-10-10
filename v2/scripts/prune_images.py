"""Keep only the newest releases' images on GHCR; the release workflow runs this after each release.

    GH_TOKEN=... python scripts/prune_images.py JackD99 officesentry [--keep 3] [--dry-run]

While the repository is private its image counts against GitHub Free's package allowance, so only the
newest ``--keep`` releases (candidates included) stay. A release is more than its tagged version: the
platform images, SBOM and provenance records under it are untagged versions of their own, pushed just
before it, and a signed attestation is a ``sha256-...`` tag pushed just after. So everything older than
an hour before the oldest kept release goes, and everything newer stays.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta

API = "https://api.github.com"
RELEASE_TAG = re.compile(r"\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?")
MARGIN = timedelta(hours=1)


def _created(version: dict) -> datetime:
    return datetime.fromisoformat(version["created_at"].replace("Z", "+00:00"))


def _tags(version: dict) -> list[str]:
    return ((version.get("metadata") or {}).get("container") or {}).get("tags") or []


def _is_release(version: dict) -> bool:
    return any(RELEASE_TAG.fullmatch(tag) for tag in _tags(version))


def to_delete(versions: list[dict], keep: int) -> list[dict]:
    """The package versions older than the newest ``keep`` releases (and their parts)."""
    releases = sorted((v for v in versions if _is_release(v)), key=_created, reverse=True)
    if len(releases) <= keep:
        return []
    cutoff = _created(releases[keep - 1]) - MARGIN
    return [v for v in versions if _created(v) < cutoff]


def _call(method: str, url: str, token: str):
    req = urllib.request.Request(  # noqa: S310 (always API, https)
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        body = resp.read()
    return json.loads(body) if body else None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("owner")
    parser.add_argument("package")
    parser.add_argument("--keep", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.keep < 1:
        parser.error("--keep must be at least 1")
    token = os.environ["GH_TOKEN"]
    base = f"{API}/users/{args.owner}/packages/container/{args.package}/versions"
    versions, page = [], 1
    while batch := _call("GET", f"{base}?per_page=100&page={page}", token):
        versions += batch
        page += 1
    doomed = to_delete(versions, args.keep)
    print(f"{len(versions)} image versions; keeping the newest {args.keep} releases; removing {len(doomed)}.")
    for version in doomed:
        tags = ",".join(_tags(version)) or "untagged"
        print(f"  {'would remove' if args.dry_run else 'removing'} {version['name']} ({tags}, {version['created_at']})")
        if not args.dry_run:
            _call("DELETE", f"{base}/{version['id']}", token)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
