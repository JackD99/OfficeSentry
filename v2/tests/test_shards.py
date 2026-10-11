"""CI's parts of the suite (``--shard K/N``, tests/conftest.py) and the check that they ran every test once
(scripts/check_shards.py)."""

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import shard_of

V2 = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_shards", V2 / "scripts" / "check_shards.py")
check_shards = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_shards)


def test_each_test_is_in_exactly_one_part_of_about_the_same_size():
    ids = [f"tests/test_{m}.py::test_{n}[{p}]" for m in range(20) for n in range(25) for p in ("a", "b")]
    for parts in (1, 2, 3, 4, 6):
        sizes = [sum(shard_of(i, parts) == k for i in ids) for k in range(1, parts + 1)]
        assert sum(sizes) == len(ids)
        assert max(sizes) - min(sizes) < len(ids) / parts * 0.2, sizes


def _write(tmp_path, name, part, parts, collected, tests):
    (tmp_path / f"{name}-{part}.json").write_text(
        json.dumps({"part": part, "parts": parts, "collected": collected, "tests": tests}), encoding="utf-8"
    )


def test_check_passes_when_the_parts_hold_every_test_once(tmp_path):
    _write(tmp_path, "tests-3.12", 1, 2, 3, ["a", "b"])
    _write(tmp_path, "tests-3.12", 2, 2, 3, ["c"])
    _write(tmp_path, "tests-3.13", 1, 1, 2, ["a", "b"])
    assert check_shards.check(list(tmp_path.glob("*.json"))) == [
        "tests-3.12: all 3 tests, each in one of 2 parts (2, 1).",
        "tests-3.13: all 2 tests, each in one of 1 parts (2).",
    ]


@pytest.mark.parametrize(
    "parts, message",
    [
        ([(1, 2, 3, ["a", "b"])], "expected parts 1 to 2, found [1]"),
        ([(1, 2, 3, ["a", "b"]), (1, 2, 3, ["c"])], "expected parts 1 to 2, found [1, 1]"),
        ([(1, 2, 3, ["a", "b"]), (2, 2, 3, ["b", "c"])], "1 of its tests ran in more than one part, such as b"),
        ([(1, 2, 3, ["a"]), (2, 2, 3, ["c"])], "the parts ran 2 of the 3 tests collected"),
        ([(1, 2, 3, ["a", "b"]), (2, 2, 4, ["c"])], "disagree"),
        ([(1, 2, 3, ["a", "b"]), (2, 3, 3, ["c"])], "disagree"),
    ],
    ids=["part-missing", "part-twice", "test-twice", "test-missing", "other-tests", "other-parts"],
)
def test_check_fails_when_a_test_is_missing_or_twice(tmp_path, parts, message):
    for n, part in enumerate(parts):
        (tmp_path / f"tests-3.12-{n}.json").write_text(
            json.dumps(dict(zip(("part", "parts", "collected", "tests"), part, strict=True))), encoding="utf-8"
        )
    with pytest.raises(ValueError, match=re.escape(message)):
        check_shards.check(list(tmp_path.glob("*.json")))


def test_check_fails_with_nothing_to_check():
    with pytest.raises(ValueError, match="No test lists"):
        check_shards.check([])


def test_pytest_runs_one_part_and_lists_it(tmp_path):
    """The real option end to end, on this file: two parts that together are what plain collection finds."""
    here = str(Path(__file__).relative_to(V2))

    def collect(*args):
        out = subprocess.run(  # noqa: S603 (this interpreter's pytest)
            [sys.executable, "-m", "pytest", "--collect-only", "-p", "no:cacheprovider", here, *args],
            cwd=V2,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return [line for line in out.splitlines() if "::" in line]

    everything = collect()
    one = collect("--shard", "1/2", "--shard-list", str(tmp_path / "tests-x-1.json"))
    two = collect("--shard", "2/2", "--shard-list", str(tmp_path / "tests-x-2.json"))
    assert sorted(one + two) == sorted(everything)
    assert check_shards.check(list(tmp_path.glob("*.json"))) == [
        f"tests-x: all {len(everything)} tests, each in one of 2 parts ({len(one)}, {len(two)})."
    ]


def test_a_bad_part_is_refused():
    result = subprocess.run(  # noqa: S603 (this interpreter's pytest)
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-p",
            "no:cacheprovider",
            "--shard",
            "3/2",
            "tests/test_shards.py",
        ],
        cwd=V2,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "--shard takes K/N" in result.stderr
