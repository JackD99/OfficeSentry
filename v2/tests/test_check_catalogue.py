"""The docs/checks.md generator runs and covers every check, collector and detail report."""

import importlib.util
from pathlib import Path

from officesentry.collectors import REGISTRY
from officesentry.reports.data_reports import REPORTS
from officesentry.rules import RULES

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gen_check_catalogue.py"


def _generator():
    spec = importlib.util.spec_from_file_location("gen_check_catalogue", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_catalogue_mentions_every_check_collector_and_report():
    text = _generator().render()
    for r in RULES:
        assert f"`{r.key}`" in text, r.key
    for key in REGISTRY:
        assert f"`{key}`" in text, key
    for key in REPORTS:
        assert f"`{key}`" in text, key


def test_catalogue_finds_the_collectors_a_check_reads():
    gen = _generator()
    checks = {r.key: r.check for r in RULES}
    # Read directly, and through a helper in another module (mailflow.tenant_domains).
    assert gen.collectors_read(checks["stale_users"]) == ["users"]
    assert {"mailboxes", "inbox_rules", "tenant_profile"} <= set(gen.collectors_read(checks["external_forwarding"]))
    assert gen.licence_notes(checks["stale_users"]) == ["Sign-in activity needs Entra ID P1"]
    assert gen.licence_notes(checks["licensed_disabled"]) == []


def test_catalogue_is_deterministic():
    gen = _generator()
    assert gen.render() == gen.render()
