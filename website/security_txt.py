"""The website's /.well-known/security.txt (RFC 9116): where to report a vulnerability, taken from the
repository's SECURITY.md so the two never disagree. Standard library only, so v2's tests can check it without
the website's own packages (v2/tests/test_security_txt.py)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

PATH = ".well-known/security.txt"
# RFC 9116 asks for an expiry less than a year ahead. Every deploy writes the file again with a new date.
LIFETIME = timedelta(days=350)
FORM = re.compile(r"\((https://github\.com/[\w.-]+/[\w.-]+/security/advisories/new)\)")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")


def security_txt(site_url: str, security_md: str, *, form: bool, now: datetime | None = None) -> str:
    """The file for the site at ``site_url``: SECURITY.md's email address, and before it GitHub's private
    reporting form when ``form`` (the repository is public, so anyone can use it)."""
    now = now or datetime.now(UTC)
    links = FORM.findall(security_md) if form else []
    emails = EMAIL.findall(security_md)
    if not emails:
        raise ValueError("SECURITY.md gives no email address for reporting a vulnerability")
    lines = [
        *(f"Contact: {link}" for link in links[:1]),
        f"Contact: mailto:{emails[0]}",
        f"Expires: {now + LIFETIME:%Y-%m-%dT%H:%M:%SZ}",
        "Preferred-Languages: en",
        f"Canonical: {site_url}/{PATH}",
        f"Policy: {site_url}/docs/security/#reporting-a-vulnerability",
    ]
    return "\n".join(lines) + "\n"
