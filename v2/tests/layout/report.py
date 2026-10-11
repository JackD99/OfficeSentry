"""output/report.md from the results each part of a run wrote (output/results-*.json), and the same as the
run's summary page in GitHub Actions. ``python -m tests.layout.report [folder]`` rewrites it by hand, for
example after downloading every part's results into one folder.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

KINDS = {
    "page-scroll": "Page scrolls sideways",
    "cut-off": "Cut off",
    "overlap": "Overlaps",
    "off-screen": "Off the screen",
    "wide-table": "Table scrolls sideways",
}


def load(folder: Path) -> list[dict]:
    rows: list[dict] = []
    for part in sorted(folder.glob("results-*.json")):
        rows += json.loads(part.read_text(encoding="utf-8"))
    return sorted(rows, key=lambda r: r["page"])


def render(rows: list[dict], *, links: bool = True) -> str:
    failing = [r for r in rows if r["found"]]
    kinds = Counter(p["kind"] for r in failing for problems in r["found"].values() for p in problems)
    lines = [
        "# Layout checks",
        "",
        f"{len(rows)} pages opened at 1366, 1440 and 1920 px wide and on a phone (390 px): "
        f"**{len(failing)} with a problem**.",
        "",
    ]
    if kinds:
        lines += ["| Problem | Times |", "|---|---|"]
        lines += [f"| {KINDS.get(k, k)} | {n} |" for k, n in kinds.most_common()]
        lines.append("")
    for r in failing:
        lines += [f"## {r['page']}", "", f"`{r['path']}`", ""]
        for width, problems in r["found"].items():
            shot = r["shots"].get(width)
            title = f"**{width}**" + (f" ([screenshot]({shot}))" if shot and links else "")
            lines.append(f"- {title}")
            for p in problems:
                times = f" ×{p['count']}" if p["count"] > 1 else ""
                lines.append(f"  - {KINDS.get(p['kind'], p['kind'])}{times}: {p['text']} ({p['where']})")
        lines.append("")
    return "\n".join(lines) + "\n"


def write(folder: Path, summary: str | None = None) -> None:
    rows = load(folder)
    if not rows:
        return
    (folder / "report.md").write_text(render(rows), encoding="utf-8")
    if summary:
        # The summary page can't show the screenshots: they're in the run's "layout" artifact.
        text = render(rows, links=False)
        if len(text) > 900_000:  # GitHub keeps a summary up to 1 MB
            text = text[:900_000] + "\n\n(cut short: the full list is report.md in the run's artifact)\n"
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text)


if __name__ == "__main__":
    write(Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent / "output"))
