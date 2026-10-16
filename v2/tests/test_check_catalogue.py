"""The docs/checks.md generator runs, covers every check, collector and detail report, and matches the file."""

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
    for key in REGISTRY:  # an add-on's collector and report are documented with the add-on
        assert (f"`{key}`" in text) == (not REGISTRY[key].addon), key
    for key, (_title, _description, sources, _builder) in REPORTS.items():
        assert (f"`{key}`" in text) == any(not REGISTRY[s].addon for s in sources), key


def test_catalogue_finds_the_collectors_a_check_reads():
    gen = _generator()
    checks = {r.key: r.check for r in RULES}
    # Read directly, and through a helper in another module (mailflow.tenant_domains).
    assert gen.collectors_read(checks["stale_users"]) == ["users"]
    assert {"mailboxes", "inbox_rules", "tenant_profile"} <= set(gen.collectors_read(checks["external_forwarding"]))
    assert gen.licence_notes(checks["stale_users"]) == ["Sign-in activity needs Entra ID P1"]
    assert gen.licence_notes(checks["licensed_disabled"]) == []
    # Said once, not again as the start of a longer reason.
    assert gen.licence_notes(checks["intune_compliance_coverage"]) == [
        "The tenant has no Intune licence, or Intune isn't readable"
    ]


def test_catalogue_is_deterministic():
    gen = _generator()
    assert gen.render() == gen.render()


def test_committed_catalogue_matches_the_code():
    committed = (SCRIPT.parents[1] / "docs" / "checks.md").read_text(encoding="utf-8")
    assert committed == _generator().render(), (
        "docs/checks.md is out of date; from v2/, run "
        "PYTHONPATH=. python scripts/gen_check_catalogue.py > docs/checks.md"
    )
