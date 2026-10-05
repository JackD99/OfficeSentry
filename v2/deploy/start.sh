#!/usr/bin/env bash
# Builds and (re)starts Office Sentry on the server, then waits until the
# portal (and the demo site, when it runs) reports healthy. The Deploy
# workflow runs it over SSH; .env's COMPOSE_PROFILES says which parts run.
set -euo pipefail
cd "${OFFICESENTRY_DIR:-/opt/officesentry/v2}"

# Build first so the portal is only down while the containers swap.
docker compose build --pull
docker compose up -d --remove-orphans

for service in web demo-web; do
  id=$(docker compose ps -q "$service")
  [ -n "$id" ] || continue
  status=starting
  for _ in $(seq 40); do
    status=$(docker inspect -f '{{.State.Health.Status}}' "$id")
    [ "$status" = healthy ] && break
    sleep 5
  done
  if [ "$status" != healthy ]; then
    docker compose logs --tail 100 "$service"
    echo "::error::$service didn't become healthy"
    exit 1
  fi
done
docker compose ps
docker image prune -f > /dev/null
