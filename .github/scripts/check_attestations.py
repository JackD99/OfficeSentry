"""Checks an image BuildKit wrote as an OCI tarball carries the SBOM and build provenance a release attaches.

    python3 .github/scripts/check_attestations.py IMAGE.tar [BUILD_CONTEXT]

IMAGE.tar is `docker/build-push-action` with `outputs: type=oci,dest=IMAGE.tar`, `sbom: true` and
`provenance: mode=max`. BUILD_CONTEXT is the `context` .github/scripts/base_image.sh wrote
(`<FROM image>=docker-image://<registry>/<image>@<digest>`); when given, the provenance must say the base
image came from that registry and digest, so BuildKit built from it rather than the Dockerfile's FROM line.
Prints a few lines of Markdown for the job summary. Standard library only.
"""

from __future__ import annotations

import json
import sys
import tarfile

ATTESTATION = "attestation-manifest"
SBOM = "https://spdx.dev/Document"
PROVENANCE = "https://slsa.dev/provenance/"


def purl_prefix(image: str) -> str:
    """How BuildKit's provenance names an image without its tag or digest: Docker Hub's by its short name."""
    for short in ("docker.io/library/", "docker.io/"):
        if image.startswith(short):
            return f"pkg:docker/{image.removeprefix(short)}@"
    return f"pkg:docker/{image}@"


def check(path: str, context: str | None) -> list[str]:
    with tarfile.open(path) as tar:
        files = {member.name.removeprefix("./"): member for member in tar.getmembers() if member.isfile()}

        def read(name: str) -> dict:
            if name not in files:
                raise ValueError(f"{name} isn't in {path}.")
            return json.load(tar.extractfile(files[name]))

        def blob(digest: str) -> dict:
            return read("blobs/" + digest.replace(":", "/", 1))

        top = read("index.json")["manifests"]
        if len(top) != 1:
            raise ValueError(f"Expected one image in {path}, found {len(top)}.")
        manifests = blob(top[0]["digest"]).get("manifests", [])
        kinds = [m.get("annotations", {}).get("vnd.docker.reference.type") for m in manifests]
        if len(manifests) != 2 or kinds.count(ATTESTATION) != 1:
            raise ValueError(f"Expected an image and its attestations, found {json.dumps(manifests, indent=1)}.")
        layers = {
            layer.get("annotations", {}).get("in-toto.io/predicate-type", ""): layer["digest"]
            for layer in blob(manifests[kinds.index(ATTESTATION)]["digest"])["layers"]
        }
        if not any(kind.startswith(SBOM) for kind in layers):
            raise ValueError(f"No SBOM among the attestations: {sorted(layers)}.")
        provenance = [kind for kind in layers if kind.startswith(PROVENANCE)]
        if len(provenance) != 1:
            raise ValueError(f"Expected one build provenance, found {sorted(layers)}.")
        predicate = blob(layers[provenance[0]])["predicate"]
        # SLSA 0.2 lists materials; SLSA 1.0 resolved dependencies.
        materials = predicate.get("materials") or predicate.get("buildDefinition", {}).get("resolvedDependencies", [])
        uris = [m.get("uri", "") for m in materials]
    lines = [f"SBOM and build provenance attached ({provenance[0]})."]
    if context:
        source = context.split("=docker-image://", 1)[1]
        image, digest = source.split("@", 1)
        base = [uri for uri in uris if uri.startswith(purl_prefix(image)) and digest.split(":", 1)[1] in uri]
        if not base:
            raise ValueError(f"The provenance doesn't name {source} among its materials: {uris}.")
        lines.append(f"The provenance names the base image as `{base[0]}`.")
    return lines


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 2
    try:
        lines = check(argv[1], argv[2] if len(argv) == 3 else None)
    except (ValueError, KeyError) as error:
        print(f"::error::{error}")
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
