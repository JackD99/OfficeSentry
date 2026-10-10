#!/usr/bin/env bash
# Keeps one GitHub issue per scheduled check (nightly.yml, canary.yml), so a failure reaches Jack as a
# notification instead of a red run nobody opens:
#
#   scheduled_issue.sh fail <label> <title> <report.md>   open the issue, or update the open one
#   scheduled_issue.sh pass <label>                         close the open issue, saying it passes again
#
# A failure that repeats the same report updates the issue quietly (the newest run's link); a different
# report adds a comment, which notifies. Only the part of the report above a "<!-- details -->" line is
# compared, so timings and other details that differ each night don't comment every time. Needs GH_TOKEN
# with issues: write, inside GitHub Actions.
set -euo pipefail

outcome=$1
label=$2
run_url="$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"
open=$(gh issue list --repo "$GITHUB_REPOSITORY" --state open --label "$label" --limit 1 \
  --json number --jq '.[0].number // empty' 2>/dev/null || true)

if [ "$outcome" = pass ]; then
  if [ -n "$open" ]; then
    gh issue close "$open" --repo "$GITHUB_REPOSITORY" --reason completed \
      --comment "Passing again in [this run]($run_url), so this is closed."
  fi
  exit 0
fi

title=$3
report=$(cat "$4")
marker="<!-- scheduled-check: $label -->"
body=$(printf '%s\n\n%s\n\n<!-- run -->\n---\nLatest failing run: %s. This issue closes by itself when the check passes again.\n' \
  "$marker" "$report" "$run_url")

if [ -z "$open" ]; then
  gh label create "$label" --repo "$GITHUB_REPOSITORY" --color d93f0b \
    --description "Opened by a scheduled check" 2>/dev/null || true
  gh issue create --repo "$GITHUB_REPOSITORY" --title "$title" --label "$label" --body "$body"
  exit 0
fi

previous=$(gh issue view "$open" --repo "$GITHUB_REPOSITORY" --json body --jq .body)
gh issue edit "$open" --repo "$GITHUB_REPOSITORY" --body "$body" > /dev/null
# Compare the reports only: the run link changes every time.
before=${previous%%<!-- run -->*}
after=${body%%<!-- run -->*}
if [ "${before%%<!-- details -->*}" != "${after%%<!-- details -->*}" ]; then
  gh issue comment "$open" --repo "$GITHUB_REPOSITORY" \
    --body "$(printf 'Something else changed in [this run](%s):\n\n%s\n' "$run_url" "$report")"
fi
