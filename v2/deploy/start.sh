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

if [ "$only" = demo ]; then
  if [[ $profiles != *,demo,* ]] || ! docker compose config --services 2>/dev/null | grep -qx demo; then
    echo "::warning::The server isn't set up for the public demo yet. Run Deploy once with the deploy action (v2/deploy/public/README.md)."
    exit 0
  fi
  docker compose up -d --no-deps demo
  services=(demo)
else
  # Build first so the portal is only down while the containers swap.
  docker compose build --pull
  docker compose up -d --remove-orphans
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
docker compose ps
docker image prune -f > /dev/null
