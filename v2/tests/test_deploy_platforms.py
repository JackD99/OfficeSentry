"""The deploy guides by platform (docs/deploy/) and the files they use: the one-file stack for container
managers and NAS systems, the Kubernetes manifests and the start-up file for a new cloud server."""

import ast
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path

import pytest
import yaml

from officesentry import __version__

V2 = Path(__file__).resolve().parent.parent
ROOT = V2.parent
GUIDES = V2 / "docs" / "deploy"
STACK = V2 / "deploy" / "stack" / "compose.yaml"
KUBERNETES = V2 / "deploy" / "kubernetes"
CLOUD_INIT = V2 / "deploy" / "cloud-init-server.yaml"
CHANGELOG = ROOT / "CHANGELOG.md"
IMAGE = "ghcr.io/jackd99/officesentry"
DIGEST = r"@sha256:[0-9a-f]{64}"


def _guides() -> list[str]:
    return sorted(p.name for p in GUIDES.glob("*.md") if p.name != "README.md")


def _current_release() -> str:
    """``__version__`` when it's a release, else (a development version) the newest release in CHANGELOG.md."""
    if re.fullmatch(r"\d+\.\d+\.\d+", __version__):
        return __version__
    return re.findall(r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}$", CHANGELOG.read_text(encoding="utf-8"), re.M)[0]


def test_guides_name_the_current_release():
    """The exact release a guide's example runs (and the Kubernetes maintenance pod) moves on with each release."""
    named = {}
    for path in [*sorted(GUIDES.glob("*.md")), KUBERNETES / "maintenance.yaml"]:
        text = path.read_text(encoding="utf-8")
        exact = r"(\d+\.\d+\.\d+)\b"  # not a moving tag such as :2
        found = re.findall(rf"^\s*OFFICESENTRY_VERSION={exact}", text, re.M) + re.findall(rf"{IMAGE}:{exact}", text)
        named.update({f"{path.name}: {version}": version for version in found})
    assert named, "no guide names a release"
    assert {where for where, version in named.items() if version != _current_release()} == set()


def test_every_guide_is_on_the_index_and_the_website():
    """A guide nobody can find: one missing from the index's table or from the website's Deploy section."""
    index = (GUIDES / "README.md").read_text(encoding="utf-8")
    tree = ast.parse((ROOT / "website" / "build.py").read_text(encoding="utf-8"))
    on_site = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert _guides(), "no guides found"
    for name in _guides():
        assert f"]({name})" in index, f"{name} isn't in the table in docs/deploy/README.md"
        assert f"v2/docs/deploy/{name}" in on_site, f"{name} isn't listed under Deploy in website/build.py"
    assert "v2/docs/deploy/README.md" in on_site


def _slug(text: str) -> str:
    """GitHub's anchor for a heading (website/build.py's github_slug), links and code marks dropped."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text).replace("`", "").strip().lower()
    text = "".join(ch for ch in text if ch in " -_" or unicodedata.category(ch)[0] in "LNM")
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    anchors, fenced = set(), False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and (heading := re.match(r"#{1,6} (.+)", line)):
            anchors.add(_slug(heading[1]))
    return anchors


DOCS_WITH_DEPLOY_LINKS = [
    *sorted(GUIDES.glob("*.md")),
    V2 / "docs" / "securing-the-portal.md",
    V2 / "DEPLOY.md",
    V2 / "README.md",
    ROOT / "README.md",
]


@pytest.mark.parametrize("doc", DOCS_WITH_DEPLOY_LINKS, ids=lambda p: str(p.relative_to(ROOT)))
def test_links_in_the_guides_lead_somewhere(doc):
    """Every relative link in the guides (and the docs that link to them) names a file that exists, and a
    heading that exists in it, so the same link works on GitHub and on the website."""
    text = re.sub(r"```.*?```", "", doc.read_text(encoding="utf-8"), flags=re.S)
    targets = re.findall(r"\]\(([^)\s]+)\)", text)
    if doc.parent != GUIDES:
        targets = [t for t in targets if "deploy/" in t]  # elsewhere only the links into the guides
    assert targets
    for target in targets:
        if re.match(r"[a-z]+:", target):
            continue
        path, _, anchor = target.partition("#")
        file = (doc.parent / path).resolve() if path else doc
        assert file.exists(), f"{doc.name} links to {target}, which doesn't exist"
        if anchor and file.suffix == ".md":
            assert anchor in _anchors(file), f"{doc.name} links to {target}, but {file.name} has no such heading"


def _stack() -> dict:
    return yaml.safe_load(STACK.read_text(encoding="utf-8"))


def test_the_stack_runs_the_published_image_fenced_in_like_the_server_setup():
    stack = _stack()["services"]
    server = yaml.safe_load((V2 / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    for name in ("web", "worker"):
        svc = stack[name]
        assert svc["image"] == f"{IMAGE}:${{OFFICESENTRY_VERSION:-2}}"
        for fence in ("read_only", "init", "tmpfs", "cap_drop", "security_opt", "pids_limit", "healthcheck"):
            assert svc[fence] == server[name][fence], f"{name}: {fence}"
        # The keys get their own volume, never the data's.
        assert svc["environment"]["OFFICESENTRY_SECRETS_FILE"] == "/keys/secrets.json"
        assert [v.rsplit(":", 1)[1] for v in svc["volumes"]] == ["/data", "/keys"]
    assert stack["worker"]["command"] == server["worker"]["command"]
    assert stack["worker"]["depends_on"]["web"] == {"condition": "service_healthy"}
    assert stack["web"]["ports"] == ["${OFFICESENTRY_PORT:-127.0.0.1:8000}:8000"]  # only the proxy reaches it


def test_the_stack_takes_its_settings_and_keys_from_the_manager_only():
    """Nothing beside the file (no env_file, no Caddyfile) and no key written in it: settings and keys come
    from the manager's own variables or secret store."""
    stack = _stack()
    for name, svc in stack["services"].items():
        assert "env_file" not in svc, name
        assert not any(v.startswith(("./", "/")) for v in svc.get("volumes", [])), f"{name} mounts a file"
    settings = stack["services"]["web"]["environment"]
    assert settings["OFFICESENTRY_BASE_URL"].startswith("${OFFICESENTRY_BASE_URL:?")  # required, says so
    assert settings["OFFICESENTRY_SECRET_KEY"] == "${OFFICESENTRY_SECRET_KEY:-}"
    assert settings["OFFICESENTRY_ENCRYPTION_KEYS"] == "${OFFICESENTRY_ENCRYPTION_KEYS:-}"
    for name in ("caddy", "cloudflared"):
        svc = stack["services"][name]
        assert re.search(DIGEST, svc["image"]), f"{name} isn't pinned by digest"
        assert svc["profiles"] and svc["read_only"] is True and svc["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in svc["security_opt"]
        assert not any("OFFICESENTRY" in v for v in svc.get("volumes", [])), f"{name} mounts the data or keys"
    assert stack["services"]["cloudflared"]["environment"]["TUNNEL_TOKEN"] == "${CLOUDFLARE_TUNNEL_TOKEN:-}"
    # Caddy's settings match v2/Caddyfile's.
    caddyfile = stack["services"]["caddy"]["environment"]["CADDYFILE"]
    for line in ("encode gzip", "header -Server", "reverse_proxy web:8000"):
        assert line in caddyfile and line in (V2 / "Caddyfile").read_text(encoding="utf-8")


def _manifests() -> dict[str, dict]:
    docs = {}
    for path in sorted(KUBERNETES.glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if doc:
                docs[f"{doc['kind']}/{doc.get('metadata', {}).get('name', path.stem)}"] = doc
    return docs


def _pod_specs(docs: dict[str, dict]) -> dict[str, dict]:
    return {
        "deployment": docs["Deployment/officesentry"]["spec"]["template"]["spec"],
        "backup": docs["CronJob/officesentry-backup"]["spec"]["jobTemplate"]["spec"]["template"]["spec"],
        "maintenance": docs["Pod/officesentry-maintenance"]["spec"],
    }


def test_kubernetes_runs_one_pod_on_one_disk():
    """One copy, stopped before the next starts, on a disk only one node mounts; backups run beside it."""
    docs = _manifests()
    deployment = docs["Deployment/officesentry"]["spec"]
    assert deployment["replicas"] == 1 and deployment["strategy"] == {"type": "Recreate"}
    containers = {c["name"]: c for c in deployment["template"]["spec"]["containers"]}
    assert set(containers) == {"web", "worker"}
    assert "python -m officesentry healthcheck web" in containers["worker"]["command"][-1]  # waits for the upgrade
    assert containers["worker"]["command"][-1].rstrip().endswith("exec python -m officesentry worker")
    for container in containers.values():
        assert all(
            p["httpGet"]["path"] == "/health" for k, p in container.items() if k.endswith("Probe") and "httpGet" in p
        )
    assert docs["PersistentVolumeClaim/officesentry-data"]["spec"]["accessModes"] == ["ReadWriteOnce"]
    backup = docs["CronJob/officesentry-backup"]["spec"]
    assert backup["concurrencyPolicy"] == "Forbid"
    affinity = backup["jobTemplate"]["spec"]["template"]["spec"]["affinity"]["podAffinity"]
    assert affinity["requiredDuringSchedulingIgnoredDuringExecution"][0]["topologyKey"] == "kubernetes.io/hostname"


def test_kubernetes_pods_are_fenced_in_and_take_the_keys_from_a_secret():
    docs = _manifests()
    assert docs["Namespace/officesentry"]["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
    for name, pod in _pod_specs(docs).items():
        assert pod["securityContext"]["runAsNonRoot"] is True, name
        assert pod["securityContext"]["runAsUser"] == 10001 and pod["securityContext"]["fsGroup"] == 10001, name
        assert pod["automountServiceAccountToken"] is False, name
        for container in pod["containers"]:
            fence = container["securityContext"]
            assert fence["readOnlyRootFilesystem"] is True and fence["allowPrivilegeEscalation"] is False, name
            assert fence["capabilities"]["drop"] == ["ALL"], name
            assert container["image"].split(":")[0] == IMAGE, name
            assert {"secretRef": {"name": "officesentry-keys"}} in container["envFrom"], name
            # Keys only ever come from the Secret: a keys file would land in the data volume.
            assert "OFFICESENTRY_SECRETS_FILE" not in str(container.get("env", "")), name
    assert "Secret/officesentry-keys" not in docs  # created by hand, never in these files


def test_kustomization_lists_what_exists_and_pins_one_release():
    kustomization = yaml.safe_load((KUBERNETES / "kustomization.yaml").read_text(encoding="utf-8"))
    for resource in kustomization["resources"]:
        assert (KUBERNETES / resource).is_file(), resource
    assert "maintenance.yaml" not in kustomization["resources"]  # applied by hand, for a restore
    (image,) = kustomization["images"]
    assert image["name"] == IMAGE and re.fullmatch(r"\d+\.\d+\.\d+", image["newTag"])
    maintenance = _manifests()["Pod/officesentry-maintenance"]["spec"]["containers"][0]["image"]
    assert maintenance == f"{IMAGE}:{image['newTag']}"
    assert image["newTag"] == _current_release()  # a release PR moves it on with __version__
    settings = dict(line.split("=", 1) for line in kustomization["configMapGenerator"][0]["literals"])
    assert settings["OFFICESENTRY_TRUSTED_PROXIES"] == "1"
    assert not any("KEY" in name for name in settings)  # keys belong in the Secret


def _install_script() -> str:
    cloud_init = yaml.safe_load(CLOUD_INIT.read_text(encoding="utf-8"))
    return next(f for f in cloud_init["write_files"] if f["path"] == "/opt/officesentry/install.sh")["content"]


def test_cloud_init_installs_the_newest_release_with_https_and_nightly_backups():
    text = CLOUD_INIT.read_text(encoding="utf-8")
    assert text.startswith("#cloud-config\n")
    cloud_init = yaml.safe_load(text)
    files = {f["path"]: f for f in cloud_init["write_files"]}
    assert '"live-restore": true' in files["/etc/docker/daemon.json"]["content"]
    assert "python -m officesentry backup --keep 14" in files["/etc/cron.d/officesentry-backup"]["content"]
    script = _install_script()
    assert files["/opt/officesentry/install.sh"]["permissions"] == "0700"
    assert "releases/latest" in script and "for f in docker-compose.yml Caddyfile env.example" in script
    assert "COMPOSE_PROFILES=https" in script and "chmod 600 .env" in script
    assert "if [ ! -e .env ]" in script  # running it again keeps the settings
    assert len(re.findall(r"# yours", script)) == 2
    assert "sh /opt/officesentry/install.sh" in cloud_init["runcmd"]


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")
def test_cloud_init_install_script_is_valid_shell(tmp_path):
    script = tmp_path / "install.sh"
    script.write_text(_install_script(), encoding="utf-8")
    result = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)  # noqa: S603, S607
    assert result.returncode == 0, result.stderr
    shellcheck = shutil.which("shellcheck")
    if shellcheck:
        result = subprocess.run([shellcheck, "--severity=style", str(script)], capture_output=True, text=True)  # noqa: S603
        assert result.returncode == 0, result.stdout
