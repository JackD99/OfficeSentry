#!/usr/bin/env bash
# Builds and (re)starts Office Sentry on the server, then waits until the
# portal (and the demo site, when it runs) reports healthy. The Deploy
# workflow runs it over SSH; .env's COMPOSE_PROFILES says which parts run.
set -euo pipefail
cd "${OFFICESENTRY_DIR:-/opt/officesentry/v2}"
# "demo": only the demo site, from an image built from ../demo-build (the Deploy
# workflow's release mode sends code that isn't a release there).
only=${1:-}
compose_file=${COMPOSE_FILE:-$(sed -n 's/^COMPOSE_FILE=//p' .env)}
profiles=${COMPOSE_PROFILES:-$(sed -n 's/^COMPOSE_PROFILES=//p' .env)}

if [ "$only" = demo ]; then
  docker build --pull -q -t officesentry:demo "${DEMO_BUILD_DIR:-../demo-build}" > /dev/null
  docker compose up -d demo-web demo-worker
  services=(demo-web)
else
  # Build first so the portal is only down while the containers swap. A release
  # (no docker-compose.build.yml in COMPOSE_FILE) was pulled already.
  if [[ "$compose_file" == *docker-compose.build.yml* ]]; then
    docker compose build --pull
  fi
  docker compose up -d --remove-orphans
  # A part switched off (say the demo) isn't an orphan to compose: remove it.
  if [[ ",$profiles," != *,demo,* ]]; then
    docker compose --profile demo rm -sf demo-web demo-worker
  fi
  services=(web demo-web)
fi

for service in "${services[@]}"; do
  id=$(docker compose ps -q "$service")
  [ -n "$id" ] || continue
  status=starting
  for _ in $(seq 40); do
    status=$(docker inspect -f '{{.State.Health.Status}}' "$id")
    [ "$status" = healthy ] && break
    sleep 5
  done
  if [ "$status" != healthy ]; then
    # Only the health check's own answer: the Deploy workflow's log will be
    # public with the repository, and the portal's log names clients and users.
    # Read that log in a shell (DEPLOY.md, "If you need a shell").
    docker inspect -f '{{range .State.Health.Log}}{{println .Output}}{{end}}' "$id" | tail -n 3
    echo "::error::$service didn't become healthy. Its log is on the server: docker compose logs $service"
    exit 1
  fi
done
# Nightly backups, the off-site copy, the restore drill and the uptime ping
# (deploy/jobs.sh), in the deploy user's crontab.
if [ "$only" != demo ] && ! bash deploy/jobs.sh schedule; then
  echo "::warning::Couldn't schedule the nightly backups (is cron installed?). DEPLOY.md, \"Off-site backups\"."
fi
docker compose ps
docker image prune -f > /dev/null
