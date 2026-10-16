#!/usr/bin/env bash
# Pulls the Docker image's base, by the digest in the Dockerfile, from the first registry that gives it, and
# says where it came from for docker/build-push-action's `build-contexts`.
#
#   .github/scripts/base_image.sh v2/Dockerfile
#
# The registries, in order: the Dockerfile's own (AWS's copy of Docker Hub's official images), Google's copy
# of Docker Hub, then Docker Hub. Runners share addresses and each registry limits what one address pulls
# anonymously; on 15 October AWS's answered 429 ("Data limit exceeded") before anything was built. The digest
# names the bytes, so the image is the same whichever answers, and the Dockerfile itself never changes.
# BuildKit keeps its own images and pulls the base again from the registry chosen here; this pull is the
# check that the registry gives it now.
#
# Writes `context=<the FROM image>=docker-image://<registry>/<image>@<digest>` to $GITHUB_OUTPUT, which
# BuildKit then builds from instead of the FROM line's registry. The docker job in v2.yml builds this way on
# every pull request, and release.yml's builds do the same.
set -euo pipefail

dockerfile=${1:?usage: base_image.sh DOCKERFILE}
from=$(sed -n 's/^FROM //p' "$dockerfile")
if [ "$(printf '%s\n' "$from" | wc -l)" != 1 ] || [ "$from" = "${from#*@sha256:}" ]; then
  echo "::error::$dockerfile needs exactly one FROM line, pinned by digest (found: $from)"
  exit 1
fi
digest=${from##*@}
repository=${from%%:*}
library=${repository#public.ecr.aws/docker/}

for source in "$repository" "mirror.gcr.io/$library" "docker.io/$library"; do
  if docker pull -q "$source@$digest" > /dev/null; then
    echo "context=$from=docker-image://$source@$digest" >> "$GITHUB_OUTPUT"
    echo "The base image came from $source." | tee -a "$GITHUB_STEP_SUMMARY"
    exit 0
  fi
  echo "::warning::$source didn't give the base image; trying the next registry"
done
echo "::error::No registry gave the base image $from"
exit 1
