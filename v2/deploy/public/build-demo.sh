#!/usr/bin/env bash
# Builds the public demo's image from the code in this folder: first the portal's own image (v2/Dockerfile),
# then the demo on top of it (Dockerfile here). The Deploy workflow copies the code it's updating the demo to
# into /opt/officesentry/demo-src (tests included, unlike the portal's copy, since the demo makes its fictional
# tenants from the test fixtures) and runs this there, so the demo never depends on the portal's version.
set -euo pipefail
cd "$(dirname "$0")/../.."
docker build --pull -q -t officesentry-demo-base:local . > /dev/null
ctx=$(mktemp -d)
trap 'rm -rf "$ctx"' EXIT
# v2/.dockerignore keeps tests out of the portal's image, so the demo's additions build from a folder of their own.
cp -r tests scripts requirements-dev.txt deploy/public/demo.sh deploy/public/Dockerfile "$ctx/"
find "$ctx" -name __pycache__ -prune -exec rm -rf {} +
docker build -q --build-arg BASE=officesentry-demo-base:local -t officesentry-demo:local "$ctx" > /dev/null
echo "Built the public demo's image."
