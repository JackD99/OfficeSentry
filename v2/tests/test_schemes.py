"""Evidence packs by scheme: certifications, frameworks and forms judged control by control from Microsoft 365 data."""

from datetime import UTC, datetime, timedelta

from officesentry import store
from officesentry.reports import evidence_answers, schemes
from officesentry.reports import schemes_cyber_essentials as ce
from officesentry.reports import schemes_forms as forms
from officesentry.reports import schemes_iasme as iasme
from officesentry.reports import schemes_iso27001 as iso
from officesentry.reports import schemes_uk_gdpr as gdpr
from officesentry.reports.schemes import FAIL, MANUAL, PARTLY, PASS, UNKNOWN
from tests import graphdata as gd
from tests.conftest import login, make_user
from tests.seed import seed_tenant
from tests.test_collectors import USERS


def _assess(db, tenant_id, key="cyber-essentials", **kw):
    return schemes.assess(db, store.get_tenant(db, tenant_id), key, **kw)


def test_registered_schemes_name_real_checks_topics_and_questions():
    assert {"cyber-essentials", "cyber-essentials-plus"} <= set(schemes.SCHEMES)
    for s in schemes.SCHEMES.values():
        assert s.version and s.source and s.intro and s.themes
        for c in s.controls:
            assert c.title and c.requirement and c.scope, c.key
            # a certification's control names the question it answers, unless the numbers change between versions
            assert c.ref or not (s.strict and s.numbered), c.key
            assert set(c.rules + c.support) <= schemes.RULE_KEYS
            assert not c.topic or c.topic in evidence_answers.KEYS
    assert ce.CYBER_ESSENTIALS.strict and ce.CYBER_ESSENTIALS_PLUS.audit
    assert [k for k, _ in schemes.grouped()] == [
        "UK certification",
        "Insurance and supplier questionnaires",
        "Data protection",
        "Frameworks and benchmarks",
    ]
    assert not forms.INSURANCE.strict and forms.INSURANCE.questions == "insurance"
    assert not forms.SUPPLIER.strict and forms.SUPPLIER.questions == "supplier"


def test_register_refuses_a_bad_scheme():
    import pytest

    good = ce.THEMES
    with pytest.raises(ValueError, match="already exists"):
        schemes.register(schemes.Scheme("cyber-essentials", "x", "x", "x", "uk", "x", good))
    bad = (schemes.Theme("t", "T", "", (schemes.Control("c", "1", "T", "R", rules=("no_such_check",)),)),)
    with pytest.raises(ValueError, match="unknown checks"):
        schemes.register(schemes.Scheme("x", "x", "x", "x", "uk", "x", bad))
    empty = (schemes.Theme("t", "T", "", (schemes.Control("c", "1", "T", "R"),)),)
    with pytest.raises(ValueError, match="no checks and no topic"):
        schemes.register(schemes.Scheme("x", "x", "x", "x", "uk", "x", empty))
    assert "x" not in schemes.SCHEMES


def test_cyber_essentials_is_all_or_nothing(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, users=USERS + [gd.INFO_USER])
    r = _assess(db, alpha)
    by = r.by_key
    # One shared mailbox allows sign-in: a few accounts, but Cyber Essentials takes no "mostly", so a Fail.
    assert by["unique_accounts"].verdict == FAIL and "info@x.com" in " ".join(by["unique_accounts"].gaps)
    assert by["unique_accounts"].fix.startswith("Block sign-in")
    # MFA enforced for everyone but a named break-glass account passes, with the exception on record.
    assert by["mfa_admins"].verdict == PASS and "break-glass" in by["mfa_admins"].text
    assert by["mfa_admins"].control.autofail
    assert by["mfa_users"].figures and any("registered" in label for label, _ in by["mfa_users"].figures)
    assert (
        by["supported_os"].verdict == FAIL and ("Windows computers out of support", "1") in by["supported_os"].figures
    )
    assert by["firewall_boundary"].verdict == MANUAL and "[" in by["firewall_boundary"].text
    assert by["passwords"].verdict == PASS and "Entra ID" in by["passwords"].text
    assert by["needed_accounts"].verdict == PARTLY  # a control the scheme judges less strictly
    assert by["m365_services"].text.startswith("Legacy authentication is allowed.")  # the gaps, when nothing passed
    assert r.headline.tone == "critical" and "to fix before applying" in r.headline.headline
    assert r.to_fix and r.to_fix == sorted(r.to_fix, key=lambda c: not c.control.autofail)
    assert r.sheet is not None and r.sheet.qset.key == "cyber-essentials"
    assert sum(r.counts.values()) == len(ce.CYBER_ESSENTIALS.controls)
    assert all(c.verdict in schemes.VERDICTS for c in r.controls)
    for t in r.themes:
        assert t.verdict in schemes.VERDICTS


def test_a_failing_check_gives_the_figures_and_the_fix(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, users=USERS + [gd.INFO_USER])
    r = _assess(db, alpha)
    failing = [c for c in r.controls if c.verdict == FAIL and c.control.rules]
    for c in failing:
        assert c.fix and c.fix_detail and c.gaps, c.control.key


def test_nothing_collected_is_not_collected(db, two_tenants):
    alpha, _ = two_tenants
    r = _assess(db, alpha)
    assert r.headline.label == "Not collected yet"
    assert all(c.verdict in (UNKNOWN, MANUAL) for c in r.controls)
    assert r.counts[UNKNOWN] > 0


def test_the_engineers_saved_answer_decides_the_verdict(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, users=USERS + [gd.INFO_USER])
    t = store.get_tenant(db, alpha)
    evidence_answers.set_override(
        db, alpha, "firewall_boundary", "Yes", "A Fortinet 60F firewall protects the office.", "Not known", "lead"
    )
    evidence_answers.set_override(db, alpha, "mfa_privileged", "Partly", "Two admins still to enrol.", "No", "lead")
    r = schemes.assess(db, t, "cyber-essentials")
    fw, mfa = r.by_key["firewall_boundary"], r.by_key["mfa_admins"]
    assert fw.verdict == PASS and fw.set_by == "lead" and fw.text.startswith("A Fortinet")
    assert mfa.verdict == FAIL  # Partly is still a Fail for a certification
    strict_off = schemes.Scheme("tmp", "t", "t", "t", "uk", "t", ce.THEMES, strict=False)
    ctx = schemes.context(db, t)
    assert schemes.judge(ce.ACCESS.controls[-2], strict_off, ctx).verdict == PARTLY


def test_ce_plus_shares_the_controls_and_predicts_each_test(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, users=USERS + [gd.INFO_USER])
    r = _assess(db, alpha, "cyber-essentials-plus")
    assert [c.control.key for c in r.controls] == [c.key for c in ce.CYBER_ESSENTIALS.controls]
    for a in ce.CE_PLUS_TESTS:
        assert all(k in r.by_key for k in a.controls)


def test_compare_shows_what_changed_since_an_earlier_day(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, users=USERS + [gd.INFO_USER])
    t = store.get_tenant(db, alpha)
    now = datetime.now(UTC)
    r = schemes.assess(db, t, "cyber-essentials", since=now - timedelta(days=365))
    assert r.since is not None and not r.since.available and "oldest data" in r.since.note
    # Pretend the first collection was a year ago, then collect again with the admin now covered by MFA.
    db.execute("UPDATE runs SET finished_at = ?, started_at = ?", ((now - timedelta(days=400)).isoformat(),) * 2)
    db.commit()
    seed_tenant(db, alpha, users=USERS)  # the info user (a guest admin) is gone
    r = schemes.assess(db, t, "cyber-essentials", since=now - timedelta(days=365))
    assert r.since.available and r.since.data_from
    assert sum(r.since.counts_then.values()) == len(r.controls)
    for c in r.since.changes:
        assert c.before != c.after


def test_assess_all_judges_every_scheme_once(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    results = schemes.assess_all(db, store.get_tenant(db, alpha))
    assert [r.scheme.key for r in results] == list(schemes.SCHEMES)


def test_forms_judge_gaps_as_partly_and_keep_the_forms_answers(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    r = _assess(db, alpha, "insurance")
    by = r.by_key
    assert by["legacy_auth"].verdict in (FAIL, PARTLY) and by["mfa_all"].figures  # judged, never strict
    assert by["backups"].verdict == MANUAL and "[" in by["backups"].text
    assert r.headline.label in ("Gaps to close", "Mostly in place") and "applying" not in r.headline.headline
    assert r.sheet is not None and r.sheet.qset.key == "insurance"
    supplier = _assess(db, alpha, "supplier")
    assert supplier.by_key["shared_accounts"].verdict in (PASS, FAIL, PARTLY)
    assert {c.control.topic for c in supplier.controls} >= {"policies", "data_location", "vulnerability_scanning"}


def test_old_links_land_on_the_scheme_pages(client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    for query, scheme in (
        ("?set=insurance", "insurance"),
        ("?set=supplier", "supplier"),
        ("?set=cyber-essentials", "cyber-essentials"),
        ("?set=controls", "insurance"),
        ("?set=nope", "insurance"),
    ):
        resp = client.get(f"/tenants/{alpha}/evidence{query}")
        assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/tenants/{alpha}/evidence/{scheme}")
    for ext in ("pdf", "xlsx", "txt", "csv"):
        resp = client.get(f"/tenants/{alpha}/evidence.{ext}?set=supplier")
        assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/evidence/supplier.{ext}")
        assert client.get(f"/tenants/{bravo}/evidence.{ext}").status_code == 404
    assert client.get(f"/tenants/{alpha}/evidence.pdf").headers["Location"].endswith("/evidence/insurance.pdf")
    assert client.get(f"/tenants/{alpha}/evidence.doc").status_code == 404


def test_a_gap_summary_says_for_example_only_before_something_named():
    control = ce.THEMES[0].controls[0]
    named = schemes.ControlResult(control, FAIL, gaps=["2 accounts.", "  ann@x.example: no MFA."])
    named.named.add(named.gaps[1])
    assert named.gap_summary == "2 accounts. For example ann@x.example: no MFA."
    whole = schemes.ControlResult(control, FAIL, gaps=["Legacy sign-in allowed.", "  no enabled policy blocks it."])
    assert whole.gap_summary == "Legacy sign-in allowed. No enabled policy blocks it."
    assert schemes.ControlResult(control, FAIL, gaps=["Only this."]).gap_summary == "Only this."


def test_every_control_links_a_real_report():
    from officesentry.reports.data_reports import REPORTS

    for s in schemes.SCHEMES.values():
        for c in s.controls:
            assert not c.report or c.report in REPORTS, (s.key, c.key, c.report)


def test_iasme_iso_and_gdpr_judge_what_microsoft_365_shows_and_ask_for_the_rest(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    assert iasme.CYBER_ASSURANCE.strict and not iasme.CYBER_ASSURANCE.numbered
    assert not iso.ISO_27001.strict and not gdpr.UK_GDPR.strict
    for key in ("iasme-cyber-assurance", "iso-27001", "uk-gdpr"):
        r = _assess(db, alpha, key)
        by = r.by_key
        assert all(c.verdict in schemes.VERDICTS for c in r.controls)
        judged = [c for c in r.controls if c.control.rules]
        assert judged and all(c.verdict in (PASS, FAIL, PARTLY, UNKNOWN) for c in judged), key
        # the processes Microsoft 365 can't see are left for the engineer, with text to complete
        outside = [c for c in r.controls if not c.control.rules and c.answer and c.answer.topic.outside]
        assert outside and all(c.verdict == MANUAL and "[" in c.text for c in outside), key
    iso_r = _assess(db, alpha, "iso-27001")
    assert iso_r.by_key["8_5"].figures  # MFA figures to quote
    assert _assess(db, alpha, "uk-gdpr").by_key["d1_breaches"].text.count("72 hours") == 1
    assert by["dpia"].verdict == MANUAL  # the last one assessed: UK GDPR


def test_new_scheme_pages_and_downloads(client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    make_user(db, "analyst", role="analyst", tenants=[alpha])
    login(client, "analyst")
    overview = client.get(f"/tenants/{alpha}/evidence").get_data(as_text=True)
    for title in ("IASME Cyber Assurance", "ISO/IEC 27001 Annex A", "UK GDPR security and accountability"):
        assert title in overview
    for key in ("iasme-cyber-assurance", "iso-27001", "uk-gdpr"):
        page = client.get(f"/tenants/{alpha}/evidence/{key}")
        assert page.status_code == 200, key
        assert client.get(f"/tenants/{bravo}/evidence/{key}").status_code == 404
        for ext in ("docx", "txt", "xlsx", "csv"):
            assert client.get(f"/tenants/{alpha}/evidence/{key}.{ext}").status_code == 200, (key, ext)
    text = client.get(f"/tenants/{alpha}/evidence/iso-27001.txt").get_data(as_text=True)
    assert "8.5" in text and "Secure authentication" in text
