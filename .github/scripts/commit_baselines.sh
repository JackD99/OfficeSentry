#!/usr/bin/env bash
# Commits a baseline refresh to a pull request's branch (refresh-baselines.yml):
#
#   commit_baselines.sh <directory> <message>
#
# The refresh ran on the pull request merged with main (what the ui and nightly checks compare), not on
# the branch alone, so a branch behind main gets baselines the checks accept. Only what the refresh
# changed under <directory> is carried onto the branch's current head: the branch keeps its own version
# of everything else, and its history gets one commit, by the maintainer (git config user.*), with
# <message>. Writes changed=true or changed=false to $GITHUB_OUTPUT. Needs $BRANCH (the branch's name).
set -euo pipefail
directory=$1
message=$2

git add -A -- "$directory"
if git diff --cached --quiet; then
  echo "The baselines in $directory already match."
  echo "changed=false" >> "$GITHUB_OUTPUT"
  exit 0
fi
git -c user.name=refresh -c user.email=refresh@localhost commit -q -m "The refresh, on the merged tree"
capture=$(git rev-parse HEAD)

git fetch -q origin "$BRANCH"
git checkout -q --detach FETCH_HEAD
git diff --no-renames --name-status -z "$capture^" "$capture" -- "$directory" |
  while IFS= read -r -d '' status && IFS= read -r -d '' path; do
    if [ "$status" = D ]; then
      git rm -q --ignore-unmatch -- "$path"
    else
      git checkout -q "$capture" -- "$path"
    fi
  done
if git diff --cached --quiet; then
  echo "The branch already has these baselines."
  echo "changed=false" >> "$GITHUB_OUTPUT"
  exit 0
fi
git diff --cached --stat
git commit -q -m "$message"
git push -q origin "HEAD:$BRANCH"
echo "changed=true" >> "$GITHUB_OUTPUT"
