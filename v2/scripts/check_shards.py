"""Check that the parts CI split the tests into ran every test exactly once.

    python scripts/check_shards.py DIR

CI (.github/workflows/v2.yml) runs the suite on each Python in parts on separate machines
(``pytest --shard K/N --shard-list FILE``, tests/conftest.py). Each part leaves a JSON file named
``<run>-<K>.json``, such as ``tests-3.12-2.json``, in DIR or a folder under it; the files of one run are
compared. This fails when a part
is missing or there twice, the parts collected different tests, a test is in two parts, or the parts
together hold fewer tests than were collected. It prints one line per run.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path


def check(files: list[Path]) -> list[str]:
    """One line per run when every run is whole; raises ValueError naming the first that isn't."""
    runs: dict[str, list[dict]] = defaultdict(list)
    for file in sorted(files):
        runs[file.stem.rsplit("-", 1)[0]].append(json.loads(file.read_text(encoding="utf-8")))
    if not runs:
        raise ValueError("No test lists were found.")
    lines = []
    for run, parts in sorted(runs.items()):
        counts = {(p["parts"], p["collected"]) for p in parts}
        if len(counts) != 1:
            raise ValueError(f"{run}: the parts disagree on how many parts or tests there are: {sorted(counts)}.")
        (expected, collected) = counts.pop()
        numbers = sorted(p["part"] for p in parts)
        if numbers != list(range(1, expected + 1)):
            raise ValueError(f"{run}: expected parts 1 to {expected}, found {numbers}.")
        seen: set[str] = set()
        for p in parts:
            twice = seen.intersection(p["tests"])
            if twice:
                raise ValueError(f"{run}: {len(twice)} of its tests ran in more than one part, such as {min(twice)}.")
            seen.update(p["tests"])
        if len(seen) != collected:
            raise ValueError(f"{run}: the parts ran {len(seen)} of the {collected} tests collected.")
        sizes = ", ".join(str(len(p["tests"])) for p in sorted(parts, key=lambda p: p["part"]))
        lines.append(f"{run}: all {collected} tests, each in one of {expected} parts ({sizes}).")
    return lines


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        lines = check(list(Path(argv[0]).rglob("*.json")))
    except ValueError as e:
        print(f"::error::{e}")
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
