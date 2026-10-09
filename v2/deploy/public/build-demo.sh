#!/usr/bin/env bash
# Builds the public demo's image (Dockerfile here) on top of the portal's image, which deploy/start.sh has
# just built. The build folder holds only what the demo adds: v2/.dockerignore keeps tests out of the portal's
# image, and the demo makes its fictional tenants from the test fixtures.
set -euo pipefail
cd "$(dirname "$0")/../.."
base=$(docker compose config --images | grep -v -e '^caddy' -e '^officesentry-demo' | head -n1)
[ -n "$base" ] || { echo "::error::No portal image to build the demo on"; exit 1; }
ctx=$(mktemp -d)
trap 'rm -rf "$ctx"' EXIT
cp -r tests scripts requirements-dev.txt deploy/public/demo.sh deploy/public/Dockerfile "$ctx/"
find "$ctx" -name __pycache__ -prune -exec rm -rf {} +
docker build -q --build-arg "BASE=$base" -t officesentry-demo:local "$ctx" > /dev/null
