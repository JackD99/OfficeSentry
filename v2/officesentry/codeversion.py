"""A hash of Office Sentry's own code, for rows stored by one version of it and read by another.

A precomputed row, a cached rollup or a search index built by older code may count, word or grade things
differently (a new check, a changed threshold), so each is stored with the code version it was built by and
rebuilt when that changes. Shared by ``reports/monthly_review.py``, ``reports/precomputed.py`` and
``search_index.py``.
"""

from __future__ import annotations

import hashlib
from functools import cache
from pathlib import Path


@cache
def code_version() -> str:
    """A short digest of every Python file in the package."""
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).resolve().parent.rglob("*.py")):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]
