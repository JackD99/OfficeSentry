#!/usr/bin/env bash
# The server's own jobs: backups, the off-site copy, the restore drill and the uptime ping. Cron runs them
# as the deploy user (every deploy installs the lines with "schedule"), and the Deploy workflow's backup,
# restore-drill and status actions run them too. DEPLOY.md, "Backups", "Off-site backups" and "Restore drill".
#
#   jobs.sh nightly    a database copy (unless one was made in the last hour), then the off-site copy
#   jobs.sh now        a database copy now, then the off-site copy
#   jobs.sh drill      restore the newest off-site copy into a scratch volume and check it opens
#   jobs.sh restore    put the newest off-site backup in place of the live database (disaster recovery)
#   jobs.sh uptime     tell the uptime monitor the portal answers (every 5 minutes, when set up)
#   jobs.sh schedule   install the cron lines for the above
#
# The off-site copy and the drill only run when .env.backup sets RESTIC_REPOSITORY; restic encrypts with
# RESTIC_PASSWORD, which lives in that file alone. Each step's result is recorded in /data/backup-status.json
# (python -m officesentry backup-status shows it), and BACKUP_PING_URL, when set, hears about every run.
set -uo pipefail
cd "${OFFICESENTRY_DIR:-/opt/officesentry/v2}" || exit 1
action=${1:-}
SCRATCH=officesentry-restore-drill
# Copies kept off-site: one a day for two weeks, one a week for two months, one a month for a year.
KEEP=(--keep-daily 14 --keep-weekly 8 --keep-monthly 12)

if [ -n "${JOBS_LOG:-}" ]; then
  # From cron: append to a log beside the app folder, keeping its last 2,000 lines.
  if [ -f "$JOBS_LOG" ]; then tail -n 2000 "$JOBS_LOG" > "$JOBS_LOG.tmp" && mv "$JOBS_LOG.tmp" "$JOBS_LOG"; fi
  exec >> "$JOBS_LOG" 2>&1
  echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) $action"
fi

# A value from one of the settings files the Deploy workflow writes (KEY='value' lines).
setting() { sed -n "s/^$1=//p" "${2:-.env.backup}" 2>/dev/null | tail -n 1 | sed "s/^'\(.*\)'$/\1/"; }
offsite_on() { [ -n "$(setting RESTIC_REPOSITORY)" ]; }
last_line() { grep -v '^[[:space:]]*$' <<<"$1" | tail -n 1; }

record() {  # part ok|failed|off words
  local text=--detail
  [ "$2" = failed ] && text=--error
  docker compose exec -T web python -m officesentry backup-status --record "$1" "--$2" "$text" "$3" \
    || echo "Couldn't record the $1 result in backup-status.json: is the portal running?"
}

fail() {  # part words
  echo "FAILED: $2"
  record "$1" failed "$2"
  return 1
}

# Healthchecks.io-style pings: the address itself for "worked", /fail for "failed".
ping_monitor() {  # url ok|fail
  [ -n "$1" ] || return 0
  local url=$1
  [ "$2" = fail ] && url="${url%/}/fail"
  curl -fsS -m 10 --retry 3 -o /dev/null "$url" || echo "Couldn't reach the monitor's ping address."
}

restic() {  # restic's own arguments, after any "docker compose run" options before a --
  local opts=()
  while [ $# -gt 0 ] && [ "$1" != -- ]; do opts+=("$1"); shift; done
  shift
  docker compose --profile offsite run --rm --no-deps -T "${opts[@]}" offsite "$@"
}

open_repository() {  # part: makes the repository on the first run
  local out code
  # restic retries an unreachable store for many minutes; two are plenty to say it's unreachable. The
  # timeout runs inside the container, so giving up leaves no container behind.
  local started=$SECONDS
  out=$(restic --entrypoint timeout -- 120 restic cat config 2>&1); code=$?
  if [ "$code" != 0 ] && [ "$code" != 10 ] && [ $((SECONDS - started)) -ge 115 ]; then
    fail "$1" "couldn't reach the off-site storage within 2 minutes (check RESTIC_REPOSITORY's address)"
    return 1
  elif [ "$code" = 10 ]; then
    echo "Creating the off-site repository (first run)."
    out=$(restic -- init 2>&1) || { fail "$1" "couldn't create the off-site repository: $(last_line "$out")"; return 1; }
  elif [ "$code" != 0 ]; then
    fail "$1" "couldn't open the off-site repository (exit $code): $(last_line "$out")"
    return 1
  fi
}

local_copy() {  # force?
  if [ "${1:-}" != force ] && docker compose exec -T web \
      find /data/backups -maxdepth 1 -name 'officesentry-*.db.gz' -mmin -60 2>/dev/null | grep -q .; then
    echo "A database copy was made in the last hour; the off-site copy takes that one."
    return 0
  fi
  # The command records its own result.
  docker compose exec -T web python -m officesentry backup --keep 14
}

offsite_copy() {
  if ! offsite_on; then
    echo "Off-site backups aren't set up (no RESTIC_REPOSITORY in .env.backup)."
    # Said in backup-status.json too, so an entry from when they were set up isn't read as overdue.
    record offsite off "not set up"
    return 0
  fi
  open_repository offsite || return 1
  local out snapshot
  # Exit 3 means some files couldn't be read: a copy was saved, but it isn't whole.
  out=$(restic -- backup /data/backups --exclude '*.part' --tag officesentry 2>&1) || {
    echo "$out" | tail -n 5
    fail offsite "the off-site copy failed: $(last_line "$out")"
    return 1
  }
  echo "$out" | tail -n 3
  snapshot=$(sed -n 's/^snapshot \([0-9a-f]*\) saved$/\1/p' <<<"$out")
  out=$(restic -- forget --tag officesentry "${KEEP[@]}" --prune 2>&1) || {
    fail offsite "copy $snapshot was saved, but removing old copies failed: $(last_line "$out")"
    return 1
  }
  record offsite ok "copy ${snapshot:-saved}"
}

# Restores the newest database backup in the newest off-site copy into the scratch volume, at
# /restore/data/backups/<name>; prints that path. With "history", the activity history copies come too, in
# /restore/data/backups/events beside it, where restore looks for them. Records failures under "part".
fetch_newest() {  # part [history]
  local out newest
  newest=$(restic -- ls latest --tag officesentry 2>/dev/null \
    | grep -E '^/data/backups/officesentry-[0-9]{8}-[0-9]{6}\.db\.gz$' | sort | tail -n 1)
  if [ -z "$newest" ]; then
    fail "$1" "the newest off-site copy holds no database backup" >&2
    return 1
  fi
  local include=(--include "$newest")
  [ "${2:-}" = history ] && include+=(--include /data/backups/events)
  echo "Restoring $newest from the newest off-site copy into a scratch volume." >&2
  docker volume rm -f "$SCRATCH" > /dev/null 2>&1
  docker volume create "$SCRATCH" > /dev/null
  if ! out=$(restic -v "$SCRATCH:/restore" -- restore latest --tag officesentry --target /restore "${include[@]}" 2>&1); then
    fail "$1" "restoring $newest from the off-site copy failed: $(last_line "$out")" >&2
    return 1
  fi
  echo "/restore$newest"
}

drill() {
  if ! offsite_on; then
    echo "Off-site backups aren't set up, so there's nothing to restore from."
    record restore_check off "off-site backups aren't set up"
    return 0
  fi
  open_repository restore_check || return 1
  local out file ok=0
  out=$(restic -- check 2>&1) || {
    fail restore_check "the off-site repository failed its check: $(last_line "$out")"
    return 1
  }
  # The same image as the portal opens it read-only, runs SQLite's integrity check and counts tenants, and
  # records the result itself. Nothing touches the live database.
  if ! file=$(fetch_newest restore_check) || ! docker compose run --rm --no-deps -T -v "$SCRATCH:/restore" web \
      python -m officesentry check-backup "$file" --record; then
    ok=1
  fi
  docker volume rm -f "$SCRATCH" > /dev/null
  return $ok
}

# Disaster recovery (DEPLOY.md, "Restoring from the off-site copy"): replaces the live database with the
# newest off-site backup. Put back secrets.json first.
restore_offsite() {
  offsite_on || { echo "Off-site backups aren't set up (no RESTIC_REPOSITORY in .env.backup)."; return 1; }
  open_repository restore_check || return 1
  local file
  file=$(fetch_newest restore_check history) || return 1
  docker compose stop web worker
  # restore checks the backup, keeps the database it replaces as backups/pre-restore-*.db.gz (or moves a
  # damaged one aside whole), and refuses a backup from a newer Office Sentry.
  if ! docker compose run --rm --no-deps -T -v "$SCRATCH:/restore" web python -m officesentry restore "$file"; then
    docker volume rm -f "$SCRATCH" > /dev/null
    # Starting now would run the portal on whatever is left, or on a new empty database if the damaged one
    # was moved aside, and the worker would start collecting into it.
    echo "FAILED: the restore didn't finish (the reason is above), so the portal and worker are left stopped."
    echo "Once the problem is fixed, run this again, or start them as they are with: docker compose up -d"
    return 1
  fi
  docker volume rm -f "$SCRATCH" > /dev/null
  docker compose up -d
}

uptime_ping() {
  local url
  url=$(setting UPTIME_PING_URL .env.uptime)
  [ -n "$url" ] || return 0
  if curl -fsS -m 10 -o /dev/null http://127.0.0.1:8000/health/ready; then
    ping_monitor "$url" ok
  else
    ping_monitor "$url" fail
  fi
}

schedule() {
  local dir=$PWD log
  log="$(dirname "$PWD")/jobs.log"
  {
    crontab -l 2>/dev/null | grep -v '# officesentry-jobs$'
    echo "45 3 * * * JOBS_LOG=$log bash $dir/deploy/jobs.sh nightly # officesentry-jobs"
    echo "30 4 * * 0 JOBS_LOG=$log bash $dir/deploy/jobs.sh drill # officesentry-jobs"
    if [ -n "$(setting UPTIME_PING_URL .env.uptime)" ]; then
      echo "*/5 * * * * bash $dir/deploy/jobs.sh uptime # officesentry-jobs"
    fi
  } | crontab -
  echo "Server jobs scheduled: backups nightly at 03:45, the restore drill on Sundays at 04:30 (server time)."
}

# One backup job at a time: a long nightly copy and the Sunday drill never overlap.
locked() {
  exec 9> "$(dirname "$PWD")/jobs.lock"
  flock -w 3600 9 || { echo "Another backup job is still running."; exit 1; }
}

case "$action" in
  nightly|now)
    locked
    status=0
    if [ "$action" = now ]; then local_copy force || status=1; else local_copy || status=1; fi
    offsite_copy || status=1
    if [ "$status" = 0 ]; then ping_monitor "$(setting BACKUP_PING_URL)" ok; else ping_monitor "$(setting BACKUP_PING_URL)" fail; fi
    exit "$status"
    ;;
  restore)
    locked
    restore_offsite || exit 1
    ;;
  drill)
    locked
    if drill; then exit 0; fi
    ping_monitor "$(setting BACKUP_PING_URL)" fail
    exit 1
    ;;
  uptime) uptime_ping ;;
  schedule) schedule ;;
  *)
    echo "Usage: $0 nightly|now|drill|restore|uptime|schedule" >&2
    exit 2
    ;;
esac
