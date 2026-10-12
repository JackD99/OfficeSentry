#!/usr/bin/env bash
# Builds and (re)starts Office Sentry on the server, then waits until the
# portal (and the public demo, when it runs) reports healthy. The Deploy
# workflow runs it over SSH; .env's COMPOSE_PROFILES says which parts run.
# "start.sh demo" restarts only the public demo, on the image that
# deploy/public/build-demo.sh has just built, and leaves the portal alone.
set -euo pipefail
cd "${OFFICESENTRY_DIR:-/opt/officesentry/v2}"
only=${1:-}
profiles=",${COMPOSE_PROFILES:-$(sed -n 's/^COMPOSE_PROFILES=//p' .env)},"

started=yes
if [ "$only" = demo ]; then
  if [[ $profiles != *,demo,* ]] || ! docker compose config --services 2>/dev/null | grep -qx demo; then
    echo "::warning::The server isn't set up for the public demo yet. Run Deploy once with the deploy action (v2/deploy/public/README.md)."
    exit 0
  fi
  docker compose up -d --no-deps demo
  services=(demo)
else
  # Build first so the portal is only down while the containers swap. A release
  # (no docker-compose.build.yml in COMPOSE_FILE) has nothing to build: it was pulled.
  docker compose build --pull
  # The worker waits for the portal to report healthy. If that fails (an upgrade slower than the health
  # check allows), the worker is left created but not started: wait for the portal below, then try again.
  docker compose up -d --remove-orphans || started=no
  # A part switched off (say the demo) isn't an orphan to compose: remove it.
  if [[ $profiles != *,demo,* ]] && docker compose --profile demo config --services 2>/dev/null | grep -qx demo; then
    docker compose --profile demo rm -sf demo
  fi
  services=(web demo)
fi

for service in "${services[@]}"; do
  id=$(docker compose ps -q "$service")
  [ -n "$id" ] || continue
  status=starting
  for _ in $(seq 200); do  # up to about 17 minutes: a database upgrade copies the whole database first
    status=$(docker inspect -f '{{.State.Health.Status}}' "$id")
    [ "$status" = healthy ] && break
    sleep 5
  done
  if [ "$status" != healthy ]; then
    # Only the health check's own answer: the Deploy workflow's log will be
    # public with the repository, and the portal's log names clients and users.
    # Read that log in a shell on the server.
    docker inspect -f '{{range .State.Health.Log}}{{println .Output}}{{end}}' "$id" | tail -n 3
    echo "::error::$service didn't become healthy. Its log is on the server: docker compose logs $service"
    exit 1
  fi
done
if [ "$only" != demo ] && [ "$started" = no ]; then
  echo "The portal is healthy now; starting the rest."
  docker compose up -d --remove-orphans
fi
# Caddy reads its Caddyfile only when it starts, and compose recreates it only when its own settings
# change, so a deploy that changed just the Caddyfile would otherwise keep the old one running.
if [ "$only" != demo ] && [[ $profiles == *,https,* ]]; then
  docker compose restart caddy
fi
# Nightly backups, the off-site copy, the restore drill and the uptime ping
# (deploy/jobs.sh), in the deploy user's crontab.
if [ "$only" != demo ] && ! bash deploy/jobs.sh schedule; then
  echo "::warning::Couldn't schedule the nightly backups (is cron installed?). DEPLOY.md, \"Off-site backups\"."
fi
docker compose ps
docker image prune -f > /dev/null
