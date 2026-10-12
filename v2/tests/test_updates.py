"""New releases and the Update button: ``updates.py`` (the release check and the files the portal and the
updater share), Settings → Updates (``web/updates.py``) and its release notes (``web/release_notes.py``).

The updater itself, which does the installing, is ``tests/test_updater.py``.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import requests
import responses

from officesentry import schedule, updates, worker
from officesentry.updates import Job, Step, Version
from officesentry.web import release_notes
from officesentry.web import updates as page
from tests.conftest import login, make_user

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
NOTES = """Faster reports and a new Teams report.

### Action required

- **Signed webhooks** change: see [v2/DEPLOY.md, "Signed webhooks"](v2/DEPLOY.md#signed-webhooks)
  and update the receiver.

### New Microsoft permissions

| Permission | API | For |
|---|---|---|
| `Channel.ReadBasic.All` | Microsoft Graph | Teams channels |

### Database upgrade

Yes, migration 41 adds a table.

### Added

- **Something new.**

### Install or update

Image: `ghcr.io/jackd99/officesentry:2.4.0`.
"""


def gh(version: str, body: str = "", **extra) -> dict:
    return {
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": False,
        "published_at": "2026-11-01T10:00:00Z",
        "html_url": f"https://github.com/JackD99/OfficeSentry/releases/tag/v{version}",
        "body": body,
        **extra,
    }


@pytest.fixture
def release_build(monkeypatch):
    """This code as release 2.2.0 (the repository's own version is a development build between releases)."""
    monkeypatch.setattr(updates, "__version__", "2.2.0")
    monkeypatch.setattr(page, "__version__", "2.2.0")


@pytest.fixture
def docker(monkeypatch):
    monkeypatch.setattr(page, "in_docker", lambda: True)


def known(db, *items) -> None:
    """GitHub's release list, as the last check stored it."""
    with responses.RequestsMock() as rsps:
        rsps.get(updates.RELEASES_URL, json=list(items))
        updates.check(db, NOW)


def updater_says(data_dir, *, seen=None, ready=True, problem="", job=None) -> None:
    """updater.json as the updater writes it."""
    folder = updates.folder(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    updates.write_json(
        folder / updates.STATUS_FILE,
        {
            "format": updates.FORMAT,
            "seen_at": (seen or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "version": "2.2.0",
            "ready": ready,
            "problem": problem,
            "install": {"project": "officesentry", "folder": "/opt/officesentry", "image": "x"},
            "job": job,
        },
    )


def job(state="running", steps=None, **extra) -> dict:
    return {
        "id": "a1b2c3d4e5f60718",
        "target": "2.4.0",
        "current": "2.2.0",
        "requested_by": "admin",
        "state": state,
        "message": extra.pop("message", ""),
        "started_at": (datetime.now(UTC) - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_at": extra.pop("finished_at", ""),
        "steps": steps or [{"key": "check", "title": "Check the release", "state": "running", "detail": ""}],
        **extra,
    }


# -- Versions and release notes --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("2.3.0", Version(2, 3, 0)),
        ("v2.10.1", Version(2, 10, 1)),
        (" 3.0.0 ", Version(3, 0, 0)),
        ("2.3", None),
        ("2.3.0rc1", None),
        ("2.3.0.dev0", None),
        ("v2.3.0-rc.1", None),
        ("", None),
        (None, None),
    ],
)
def test_versions_are_releases_only(text, parsed):
    assert Version.parse(text) == parsed


def test_versions_order_by_number_not_text():
    assert Version.parse("2.10.0") > Version.parse("2.9.9") > Version.parse("2.9.0")
    assert str(Version(2, 10, 0)) == "2.10.0"


def test_a_development_build_is_never_offered_an_update(db, monkeypatch):
    monkeypatch.setattr(updates, "__version__", "2.3.0.dev0")
    known(db, gh("2.4.0"))
    assert updates.running() is None
    assert updates.newer(db) == [] and updates.available(db) is None


def test_notes_keep_the_summary_and_what_to_read_before_updating():
    summary, sections = updates.parse_notes(NOTES)
    assert summary == "Faster reports and a new Teams report."
    assert set(sections) == {"action", "permissions", "database"}
    assert "Signed webhooks" in sections["action"] and "update the receiver" in sections["action"]
    assert "Channel.ReadBasic.All" in sections["permissions"]
    # Added, Changed and Fixed stay on GitHub, and so does the workflow's own install note.
    assert "Something new" not in json.dumps(sections) and "ghcr.io" not in json.dumps(sections)


def test_a_database_upgrade_is_read_from_its_section():
    yes = updates.from_github(gh("2.4.0", NOTES))
    no = updates.from_github(gh("2.4.1", "### Database upgrade\n\nNo.\n"))
    assert yes.upgrades_database and not no.upgrades_database
    assert not updates.from_github(gh("2.4.2", "Nothing to say.")).upgrades_database


def test_an_action_required_of_none_asks_nothing():
    """Every release fills in Action required (CONTRIBUTING.md), saying "None" when there's nothing to do."""
    none = updates.from_github(gh("2.4.1", "### Action required\n\nNone. `docker-compose.yml` is the same.\n"))
    assert none.action and not none.asks
    assert updates.from_github(gh("2.4.0", NOTES)).asks
    assert not updates.from_github(gh("2.4.2", "Nothing to say.")).asks


def test_drafts_pre_releases_and_other_tags_are_not_releases():
    assert updates.from_github(gh("2.4.0", draft=True)) is None
    assert updates.from_github(gh("2.4.0", prerelease=True)) is None
    assert updates.from_github({**gh("2.4.0"), "tag_name": "v2.4.0-rc.1"}) is None
    assert updates.from_github({**gh("2.4.0"), "tag_name": "nightly"}) is None
    assert updates.from_github("not a release") is None
    # A link anywhere but this repository's releases is replaced with the release's own page.
    odd = updates.from_github({**gh("2.4.0"), "html_url": "https://example.com/phish"})
    assert odd.url == "https://github.com/JackD99/OfficeSentry/releases/tag/v2.4.0"


def test_very_long_notes_are_cut():
    _, sections = updates.parse_notes("### Action required\n\n" + "- a step\n" * 5000)
    assert len(sections["action"]) <= updates.SECTION_CHARS + 3 and sections["action"].endswith("…")


# -- The release check -------------------------------------------------------------------------------------


@responses.activate
def test_the_check_keeps_releases_newest_first(db, release_build):
    responses.get(updates.RELEASES_URL, json=[gh("2.3.0"), gh("2.10.0"), gh("2.4.0", draft=True), gh("2.1.0")])
    found = updates.check(db, NOW)
    assert [r.version for r in found] == ["2.10.0", "2.3.0", "2.1.0"]
    assert [r.version for r in updates.releases(db)] == ["2.10.0", "2.3.0", "2.1.0"]
    assert [r.version for r in updates.newer(db)] == ["2.10.0", "2.3.0"]
    assert updates.available(db).version == "2.10.0"
    state = updates.check_state(db)
    assert state.on and state.checked_at == "2026-11-02T09:00:00Z" and state.error is None
    sent = responses.calls[0].request
    assert sent.headers["User-Agent"].startswith("OfficeSentry/") and "per_page=30" in sent.url


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        ({"status": 403, "json": {"message": "API rate limit exceeded"}}, "limit"),
        ({"status": 500, "body": "oops"}, "GitHub answered 500."),
        ({"status": 200, "body": "<html>"}, "wasn't readable"),
        ({"status": 200, "json": {"message": "not a list"}}, "wasn't a list"),
        ({"body": requests.ConnectionError("down")}, "couldn't be reached (ConnectionError)"),
    ],
)
def test_a_failed_check_says_why_and_keeps_what_it_had(db, release_build, answer, said):
    known(db, gh("2.3.0"))
    with responses.RequestsMock() as rsps, pytest.raises(updates.CheckFailed):
        rsps.get(updates.RELEASES_URL, **answer)
        updates.check(db, NOW + timedelta(hours=1))
    assert said in updates.check_state(db).error
    assert [r.version for r in updates.releases(db)] == ["2.3.0"]


def test_the_check_runs_daily_and_hourly_after_a_failure(db):
    assert updates.due(db, NOW)  # never checked
    known(db, gh("2.3.0"))
    assert not updates.due(db, NOW + timedelta(hours=23))
    assert updates.due(db, NOW + timedelta(hours=24))
    with responses.RequestsMock() as rsps:
        rsps.get(updates.RELEASES_URL, status=502)
        with pytest.raises(updates.CheckFailed):
            updates.check(db, NOW)
    assert updates.due(db, NOW + timedelta(hours=1)) and not updates.due(db, NOW + timedelta(minutes=50))
    updates.set_checking(db, False)
    assert not updates.checking(db) and not updates.due(db, NOW + timedelta(days=30))


def test_the_worker_checks_when_due_and_records_finished_updates(app, cfg, db, release_build, monkeypatch):
    monkeypatch.setattr(cfg, "testing", False)  # other tests' workers never ask GitHub
    tick = lambda at: schedule.run_periodic(schedule.TickContext(app=app, conn=db, vault=None, cfg=cfg, now=at))  # noqa: E731
    with responses.RequestsMock() as rsps:
        rsps.get(updates.RELEASES_URL, json=[gh("2.3.0")])
        tick(NOW)
        assert len(rsps.calls) == 1
        tick(NOW + timedelta(minutes=1))  # not due again for a day
        assert len(rsps.calls) == 1
    assert updates.available(db).version == "2.3.0"

    # GitHub down: logged and tried again later, never an error in the worker.
    with responses.RequestsMock() as rsps:
        rsps.get(updates.RELEASES_URL, body=requests.ConnectionError("down"))
        tick(NOW + timedelta(days=1, minutes=1))
    assert "couldn't be reached" in updates.check_state(db).error

    updater_says(cfg.data_dir, job=job("done", message="Office Sentry 2.4.0 is running.", finished_at="x"))
    tick(NOW + timedelta(days=1, minutes=2))
    tick(NOW + timedelta(days=1, minutes=3))
    rows = db.execute("SELECT username, action, detail FROM audit_log WHERE action LIKE 'update_%'").fetchall()
    assert [tuple(r) for r in rows] == [
        ("admin", "update_installed", "2.2.0 to 2.4.0: Office Sentry 2.4.0 is running.")
    ]


def test_the_public_demo_never_checks(app, cfg, db, monkeypatch):
    monkeypatch.setattr(cfg, "public_demo", True)
    with responses.RequestsMock():  # any request would fail the test
        schedule.run_periodic(schedule.TickContext(app=app, conn=db, vault=None, cfg=cfg, now=NOW))


@pytest.mark.parametrize(
    ("state", "action"),
    [
        ("done", "update_installed"),
        ("rolled_back", "update_rolled_back"),
        ("failed", "update_failed"),
        ("refused", "update_refused"),
    ],
)
def test_each_outcome_is_recorded_once(cfg, db, state, action):
    updater_says(cfg.data_dir, job=job("running"))
    assert not updates.record_finished(db, cfg.data_dir)  # still running
    updater_says(cfg.data_dir, job=job(state, message="Why."))
    assert updates.record_finished(db, cfg.data_dir)
    assert not updates.record_finished(db, cfg.data_dir)
    assert db.execute("SELECT action FROM audit_log WHERE action LIKE 'update_%'").fetchall()[0][0] == action


# -- The files the portal and the updater share --------------------------------------------------------------


def test_a_request_is_written_whole_and_read_back(cfg):
    job_id = updates.request_update(cfg.data_dir, Version(2, 4, 0), "admin", NOW)
    folder = updates.folder(cfg.data_dir)
    assert oct(folder.stat().st_mode & 0o777) == "0o700"
    assert [p.name for p in folder.iterdir()] == [updates.REQUEST_FILE]  # no temporary file left behind
    asked = updates.read_request(cfg.data_dir)
    assert (asked.id, asked.version, asked.by, asked.at) == (job_id, "2.4.0", "admin", "2026-11-02T09:00:00Z")
    state = updates.status(cfg.data_dir, NOW)
    assert updates.waiting(cfg.data_dir, state, NOW + timedelta(minutes=9)) == asked
    assert updates.waiting(cfg.data_dir, state, NOW + timedelta(minutes=11)) is None  # the updater refuses it now
    updater_says(cfg.data_dir, job=job(id=job_id))
    assert updates.waiting(cfg.data_dir, updates.status(cfg.data_dir), NOW) is None  # picked up


def test_files_that_are_odd_are_ignored(cfg, tmp_path):
    folder = updates.folder(cfg.data_dir)
    folder.mkdir(parents=True)
    status = folder / updates.STATUS_FILE
    for text in ("not json", json.dumps(["a list"]), json.dumps({"format": 99, "ready": True})):
        status.write_text(text, encoding="utf-8")
        assert updates.status(cfg.data_dir) == updates.Status()
    status.write_text(json.dumps({"format": 1, "pad": "x" * updates.MAX_FILE_BYTES}), encoding="utf-8")
    assert updates.read_json(status) is None
    status.unlink()
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"format": 1, "ready": True, "seen_at": "2026-11-02T09:00:00Z"}), encoding="utf-8")
    os.symlink(elsewhere, status)
    assert updates.read_json(status) is None  # never followed
    (folder / updates.REQUEST_FILE).write_text(
        json.dumps({"format": 1, "id": "../../etc", "version": "2.4.0"}), encoding="utf-8"
    )
    assert updates.read_request(cfg.data_dir) is None


def test_the_updater_counts_as_running_while_it_checks_in(cfg):
    updater_says(cfg.data_dir, seen=NOW)
    assert updates.status(cfg.data_dir, NOW + timedelta(seconds=80)).connected
    gone = updates.status(cfg.data_dir, NOW + timedelta(seconds=100))
    assert not gone.connected and gone.seen_at == "2026-11-02T09:00:00Z"


def test_a_job_reads_back_from_its_file():
    loaded = Job.load(job("rolled_back", steps=[{"key": "health", "title": "Check", "state": "failed", "detail": "x"}]))
    assert loaded.finished and loaded.step == Step("health", "Check", "failed", "x")
    assert Job.load({"id": "x", "steps": [{"bogus": 1}]}) is None
    assert Job.load("nope") is None


# -- Release notes on the page -------------------------------------------------------------------------------


def test_notes_are_escaped_and_links_kept_safe():
    html = release_notes.render(
        "Use <script>alert(1)</script> **carefully**, see [DEPLOY](v2/DEPLOY.md#updating), "
        "[evil](javascript:alert(1)), [up](../../secrets) and [GitHub](https://github.com/JackD99/OfficeSentry). "
        "`a **b** <c>` and *this*.",
        "2.4.0",
    )
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<strong>carefully</strong>" in html and "<em>this</em>" in html
    assert (
        '<a href="https://github.com/JackD99/OfficeSentry/blob/v2.4.0/v2/DEPLOY.md#updating" rel="noopener">DEPLOY</a>'
        in html
    )
    assert "javascript:" not in html and ">evil<" not in html and "evil" in html
    assert "../../secrets" not in html
    assert '<a href="https://github.com/JackD99/OfficeSentry" rel="noopener">GitHub</a>' in html
    assert "<code>a **b** &lt;c&gt;</code>" in html


def test_notes_draw_lists_and_tables():
    html = release_notes.render(updates.parse_notes(NOTES)[1]["action"], "2.4.0")
    assert html.count("<li>") == 1 and "update the receiver" in html  # the continuation line joins its item
    table = release_notes.render(updates.parse_notes(NOTES)[1]["permissions"], "2.4.0")
    assert "<th>Permission</th>" in table and "<td><code>Channel.ReadBasic.All</code></td>" in table
    assert "---" not in table


# -- Settings → Updates ------------------------------------------------------------------------------------


@pytest.fixture
def admin(client, db):
    make_user(db, "admin", "admin")
    login(client, "admin")
    return client


def test_up_to_date(admin, db, release_build):
    page_ = admin.get("/admin/updates").get_data(as_text=True)
    assert "Not checked yet" in page_
    known(db, gh("2.2.0"), gh("2.1.0"))
    page_ = admin.get("/admin/updates").get_data(as_text=True)
    assert "You're up to date" in page_ and "Up to date" in page_ and "Update to" not in page_


def test_a_development_build_says_so(admin, db, monkeypatch):
    monkeypatch.setattr(updates, "__version__", "2.3.0.dev0")
    monkeypatch.setattr(page, "__version__", "2.3.0.dev0")
    known(db, gh("2.4.0", NOTES))
    page_ = admin.get("/admin/updates").get_data(as_text=True)
    assert "A development build" in page_ and "Update to" not in page_


def test_a_release_shows_what_it_asks_and_how_to_turn_the_updater_on(admin, db, release_build, docker):
    known(db, gh("2.4.0", NOTES), gh("2.3.0", "### Action required\n\n- Change the proxy.\n"), gh("2.2.0"))
    page_ = admin.get("/admin/updates").get_data(as_text=True)
    assert "Office Sentry 2.4.0" in page_ and "2.4.0 available" in page_
    assert "2 releases since 2.2.0" in page_ and "Change the proxy." in page_  # 2.3.0's notes too
    assert "Channel.ReadBasic.All" in page_ and "migration 41" in page_
    assert "blob/v2.4.0/v2/DEPLOY.md#signed-webhooks" in page_
    assert "Something new" not in page_
    # No updater yet: how to turn it on, and no button.
    assert "COMPOSE_PROFILES=updater" in page_ and "Update to 2.4.0" not in page_


def test_the_button_when_the_updater_is_ready(admin, cfg, db, release_build, docker):
    known(db, gh("2.4.0", NOTES))
    updater_says(cfg.data_dir)
    page_ = admin.get("/admin/updates").get_data(as_text=True)
    assert "Update to 2.4.0" in page_ and 'name="read" value="1" required' in page_
    assert "database included" in page_  # 2.4.0 upgrades the database
    assert "Ready" in page_ and "/opt/officesentry" in page_


@pytest.mark.parametrize(
    ("status", "said"),
    [
        (
            {"ready": False, "problem": "This server builds Office Sentry from its source."},
            "builds Office Sentry from its source",
        ),
        ({"seen": datetime.now(UTC) - timedelta(hours=2)}, "docker compose up -d updater"),
    ],
)
def test_no_button_when_the_updater_cant(admin, cfg, db, release_build, docker, status, said):
    known(db, gh("2.4.0"))
    updater_says(cfg.data_dir, **status)
    page_ = admin.get("/admin/updates").get_data(as_text=True)
    assert said in page_ and "Update to 2.4.0" not in page_


def test_a_new_major_version_is_installed_by_hand(admin, cfg, db, release_build, docker):
    known(db, gh("3.0.0", "### Action required\n\n- New compose file.\n"))
    updater_says(cfg.data_dir)
    page_ = admin.get("/admin/updates").get_data(as_text=True)
    assert "is a new major version" in page_ and "New compose file." in page_ and "Update to 3.0.0" not in page_
    # With a 2.x release out too, the button offers that, and the new major version is mentioned.
    known(db, gh("3.0.0"), gh("2.4.0"))
    page_ = admin.get("/admin/updates").get_data(as_text=True)
    assert "Update to 2.4.0" in page_ and "Also out:" in page_ and "3.0.0</a>, a new major version" in page_


def test_pressing_update_asks_the_updater(admin, cfg, db, release_build, docker):
    known(db, gh("2.4.0", NOTES))
    updater_says(cfg.data_dir)
    refused = admin.post("/admin/updates/start", data={"version": "2.4.0"}, follow_redirects=True)
    assert "read Action required first" in refused.get_data(as_text=True)
    assert updates.read_request(cfg.data_dir) is None
    admin.post("/admin/updates/start", data={"version": "2.4.0", "read": "1"})
    asked = updates.read_request(cfg.data_dir)
    assert (asked.version, asked.by) == ("2.4.0", "admin")
    audit = db.execute("SELECT username, detail FROM audit_log WHERE action = 'update_requested'").fetchone()
    assert tuple(audit) == ("admin", "2.2.0 to 2.4.0")
    # The page now waits for the updater, and a second press doesn't ask again.
    page_ = admin.get("/admin/updates").get_data(as_text=True)
    assert "Waiting for the updater" in page_ and "data-update-progress" in page_
    again = admin.post("/admin/updates/start", data={"version": "2.4.0", "read": "1"}, follow_redirects=True)
    assert "be started now; this page says why" in again.get_data(as_text=True)
    assert updates.read_request(cfg.data_dir).id == asked.id


def test_a_release_that_asks_nothing_needs_no_tick(admin, cfg, db, release_build, docker):
    """ "None" under Action required is shown as it is, without the warning or the box to tick, unless a release
    the update also brings asks something."""
    nothing = "### Action required\n\nNone. `docker-compose.yml` is the same as 2.3.0's.\n"
    warning = '<div class="grow notes"><strong>Action required</strong>'
    known(db, gh("2.4.0", nothing))
    updater_says(cfg.data_dir)
    page_ = admin.get("/admin/updates").get_data(as_text=True)
    assert "<h4>Action required</h4>" in page_ and "is the same as 2.3.0" in page_ and "Update to 2.4.0" in page_
    assert warning not in page_ and 'name="read"' not in page_
    known(db, gh("2.4.0", nothing), gh("2.3.0", "### Action required\n\n- Change the proxy.\n"))
    page_ = admin.get("/admin/updates").get_data(as_text=True)
    assert page_.count(warning) == 1 and 'name="read" value="1" required' in page_
    known(db, gh("2.4.0", nothing))
    admin.post("/admin/updates/start", data={"version": "2.4.0"})
    assert updates.read_request(cfg.data_dir).version == "2.4.0"


@pytest.mark.parametrize("version", ["2.3.0", "2.5.0", "1.0.0"])
def test_only_the_offered_release_can_be_asked_for(admin, cfg, db, release_build, docker, version):
    known(db, gh("2.4.0"), gh("2.3.0"))
    updater_says(cfg.data_dir)
    admin.post("/admin/updates/start", data={"version": version, "read": "1"})
    assert updates.read_request(cfg.data_dir) is None
    assert admin.post("/admin/updates/start", data={"version": "latest"}).status_code == 400


def test_an_update_under_way_shows_its_steps(admin, cfg, db, release_build, docker):
    known(db, gh("2.4.0"))
    steps = [
        {"key": "check", "title": "Check the release", "state": "done", "detail": "Release 2.4.0"},
        {"key": "verify", "title": "Verify its signature", "state": "running", "detail": "Checking…"},
        {"key": "download", "title": "Download it", "state": "pending", "detail": ""},
    ]
    updater_says(cfg.data_dir, job=job("running", steps=steps))
    page_ = admin.get("/admin/updates").get_data(as_text=True)
    assert "Updating to 2.4.0" in page_ and '<li class="current"><div class="step-title">Verify its signature' in page_
    assert '<li class="done"><div class="step-title">Check the release' in page_ and "Update to 2.4.0" not in page_
    before = admin.get("/admin/updates/progress").get_json()
    assert before["busy"] and before["signature"] in page_
    steps[1]["state"] = "done"
    updater_says(cfg.data_dir, job=job("running", steps=steps))
    assert admin.get("/admin/updates/progress").get_json()["signature"] != before["signature"]


def test_the_worker_restarting_mid_update_isnt_an_alarm_on_the_page(admin, cfg, db, release_build, docker, monkeypatch):
    monkeypatch.setattr(worker, "is_alive", lambda conn: False)
    known(db, gh("2.4.0"))
    updater_says(cfg.data_dir, job=job("running"))
    assert "background worker isn" not in admin.get("/admin/updates").get_data(as_text=True)
    assert "background worker isn" in admin.get("/admin/users").get_data(as_text=True)


@pytest.mark.parametrize(
    ("state", "shown"),
    [
        ("done", "Updated"),
        ("rolled_back", "Rolled back"),
        ("failed", "Rolling back an update"),
        ("refused", "Not started"),
    ],
)
def test_the_outcome_leads_the_page_for_a_day(admin, cfg, db, release_build, docker, state, shown):
    known(db, gh("2.4.0"))
    finished = datetime.now(UTC) - timedelta(minutes=5)
    updater_says(
        cfg.data_dir, job=job(state, message="The outcome.", finished_at=finished.strftime("%Y-%m-%dT%H:%M:%SZ"))
    )
    page_ = admin.get("/admin/updates").get_data(as_text=True)
    assert "lead-answer" in page_ and "The outcome." in page_ and shown in page_
    old = (finished - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    updater_says(cfg.data_dir, job=job(state, message="The outcome.", finished_at=old))
    page_ = admin.get("/admin/updates").get_data(as_text=True)
    assert "lead-answer" not in page_ and "Last update: 2.2.0 to 2.4.0" in page_


def test_a_request_the_updater_never_saw(admin, cfg, db, release_build, docker):
    known(db, gh("2.4.0"))
    updates.request_update(cfg.data_dir, Version(2, 4, 0), "admin", datetime.now(UTC) - timedelta(minutes=30))
    page_ = admin.get("/admin/updates").get_data(as_text=True)
    assert "didn't pick up the request for 2.4.0" in page_


def test_check_now_and_the_daily_check_setting(admin, db, release_build):
    with responses.RequestsMock() as rsps:
        rsps.get(updates.RELEASES_URL, json=[gh("2.4.0")])
        shown = admin.post("/admin/updates/check", follow_redirects=True).get_data(as_text=True)
    assert "Office Sentry 2.4.0 is available." in shown
    with responses.RequestsMock() as rsps:
        rsps.get(updates.RELEASES_URL, status=500)
        shown = admin.post("/admin/updates/check", follow_redirects=True).get_data(as_text=True)
    assert "check for new releases: GitHub answered 500." in shown
    admin.post("/admin/updates/settings", data={})
    assert not updates.checking(db)
    admin.post("/admin/updates/settings", data={"check": "on"})
    assert updates.checking(db)
    rows = db.execute("SELECT detail FROM audit_log WHERE action = 'update_check_changed' ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["off", "on"]


def test_the_menu_says_when_a_release_is_out(admin, client, db, release_build):
    known(db, gh("2.4.0"))
    assert "2.4.0 is available" in admin.get("/admin/support").get_data(as_text=True)
    make_user(db, "analyst", "analyst", all_tenants=True)
    client.post("/logout")
    login(client, "analyst")
    home = client.get("/today").get_data(as_text=True)
    assert "is available" not in home
    assert client.get("/admin/updates").status_code == 403


def test_the_pages_links_into_deploy_md_land_on_its_headings():
    """DEPLOY.md is the website's Install page, with GitHub's anchors (website/build.py)."""
    text = (Path(__file__).parents[1] / "DEPLOY.md").read_text(encoding="utf-8")
    anchors = {re.sub(r"[^\w\- ]", "", h.lower()).replace(" ", "-") for h in re.findall(r"^#+ (.+)$", text, re.M)}
    for url in (page.DEPLOY_UPDATING, page.DEPLOY_BUTTON, page.DEPLOY_ROLLBACK):
        assert url.startswith(updates.DEPLOY_DOCS) and url.split("#")[1] in anchors
