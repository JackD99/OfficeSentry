"""The website's /.well-known/security.txt (RFC 9116), made by website/security_txt.py from SECURITY.md, and
the deploy that has to carry a file in a hidden folder to the server."""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SECURITY_MD = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
SITE = "https://officesentry.co.uk"
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


def _module():
    spec = importlib.util.spec_from_file_location("security_txt", ROOT / "website" / "security_txt.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fields(text: str) -> list[tuple[str, str]]:
    assert text.endswith("\n")
    return [tuple(line.split(": ", 1)) for line in text.splitlines()]


def test_security_txt_says_where_to_report_a_vulnerability():
    fields = _fields(_module().security_txt(SITE, SECURITY_MD, form=True, now=NOW))
    contacts = [v for k, v in fields if k == "Contact"]
    assert contacts == [
        "https://github.com/JackD99/OfficeSentry/security/advisories/new",  # SECURITY.md's first choice first
        "mailto:hello@officesentry.co.uk",
    ]
    values = dict(fields)
    assert NOW < datetime.fromisoformat(values["Expires"]) < NOW + timedelta(days=365)  # less than a year ahead
    assert values["Canonical"] == f"{SITE}/.well-known/security.txt"
    assert values["Policy"] == f"{SITE}/docs/security/#reporting-a-vulnerability"
    assert "## Reporting a vulnerability" in SECURITY_MD  # the heading the Policy link opens at
    assert values["Preferred-Languages"] == "en"


def test_a_private_repositorys_form_isnt_offered():
    """Nobody outside can open the reporting form of a private repository, so only the email is given."""
    fields = _fields(_module().security_txt(SITE, SECURITY_MD, form=False, now=NOW))
    assert [v for k, v in fields if k == "Contact"] == ["mailto:hello@officesentry.co.uk"]


def test_the_site_build_writes_it_and_the_deploy_carries_it():
    build = (ROOT / "website" / "build.py").read_text(encoding="utf-8")
    assert "(OUT / SECURITY_TXT).write_text(" in build and 'form=cfg["source_public"]' in build
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8"))
    (upload,) = [s for s in workflow["jobs"]["website"]["steps"] if s.get("with", {}).get("name") == "website"]
    assert upload["with"]["include-hidden-files"] is True  # .well-known is a hidden folder
