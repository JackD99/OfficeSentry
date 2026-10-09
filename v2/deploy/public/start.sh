#!/usr/bin/env bash
# Builds and (re)starts the public server: the website and the demo (README.md here). The Deploy workflow
# copies v2/ to /opt/officesentry/v2 and the built website to /opt/officesentry/site, then runs this over SSH.
set -euo pipefail
v2=/opt/officesentry/v2
cd "$v2/deploy/public"

# The portal's own image, then the demo on top of it, from a folder holding only what the demo adds
# (v2/.dockerignore leaves tests out of the portal's image, and the demo needs them for its fixtures).
docker build --pull -q -t officesentry-app:local "$v2" > /dev/null
ctx=$(mktemp -d)
trap 'rm -rf "$ctx"' EXIT
cp -r "$v2/tests" "$v2/scripts" "$v2/requirements-dev.txt" demo.sh Dockerfile "$ctx/"
rm -rf "$ctx/tests/__pycache__" "$ctx/scripts/__pycache__"
docker build -q --build-arg BASE=officesentry-app:local -t officesentry-demo:local "$ctx" > /dev/null

docker compose up -d --remove-orphans --force-recreate demo
docker compose up -d --remove-orphans caddy
# Caddy reads the Caddyfile at start; reload picks up a changed one without dropping connections.
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile > /dev/null 2>&1 || docker compose restart caddy

id=$(docker compose ps -q demo)
status=starting
for _ in $(seq 40); do
  status=$(docker inspect -f '{{.State.Health.Status}}' "$id")
  [ "$status" = healthy ] && break
  sleep 5
done
if [ "$status" != healthy ]; then
  docker inspect -f '{{range .State.Health.Log}}{{println .Output}}{{end}}' "$id" | tail -n 3
  echo "::error::The demo didn't become healthy. Its log is on the server: cd $v2/deploy/public && docker compose logs demo"
  exit 1
fi
docker compose ps --format 'table {{.Service}}\t{{.Status}}'
docker image prune -f > /dev/null
