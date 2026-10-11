"""Creates or updates the repository's labels from .github/labels.yml, with the gh CLI.

    python3 .github/scripts/sync_labels.py            # inside GitHub Actions (GH_TOKEN with issues: write)
    python3 .github/scripts/sync_labels.py --dry-run  # say what would change, change nothing

Every label in the file ends up on GitHub with that name, colour and description; labels on GitHub that the file
doesn't list are left alone and listed, so a label somebody made by hand is never deleted by a push. Needs
PyYAML (the workflow installs it) and GITHUB_REPOSITORY, or --repo owner/name.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

LABELS = Path(__file__).resolve().parent.parent / "labels.yml"


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout  # noqa: S603, S607


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), help="owner/name")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.repo:
        parser.error("--repo owner/name is needed outside GitHub Actions")

    wanted = yaml.safe_load(LABELS.read_text(encoding="utf-8"))
    names = [label["name"] for label in wanted]
    if len(set(names)) != len(names):
        sys.exit(f"{LABELS.name} lists a label twice.")
    for label in wanted:
        if not (isinstance(label.get("color"), str) and len(label["color"]) == 6):
            sys.exit(f"Label {label.get('name')!r} needs a six-digit hex colour, like d73a4a.")

    existing = {
        label["name"]: label
        for label in json.loads(
            gh("label", "list", "--repo", args.repo, "--limit", "500", "--json", "name,color,description")
        )
    }
    created = updated = 0
    for label in wanted:
        current = existing.get(label["name"])
        same = (
            current is not None
            and current["color"].lower() == label["color"].lower()
            and (current.get("description") or "") == label.get("description", "")
        )
        if same:
            continue
        verb = "update" if current else "create"
        print(f"{verb} {label['name']!r}: #{label['color']}, {label.get('description', '')!r}")
        if not args.dry_run:
            gh(
                "label",
                "create",
                label["name"],
                "--repo",
                args.repo,
                "--force",
                "--color",
                label["color"],
                "--description",
                label.get("description", ""),
            )
        created += verb == "create"
        updated += verb == "update"

    extra = sorted(set(existing) - set(names))
    summary = [f"Labels: {created} created, {updated} updated, {len(names) - created - updated} already right."]
    if extra:
        summary.append("On GitHub but not in labels.yml (left alone): " + ", ".join(f"`{name}`" for name in extra))
    print("\n".join(summary))
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write("\n".join(summary) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
