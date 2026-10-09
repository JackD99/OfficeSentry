"""The public demo site (web/public_demo.py): visitors are the read-only demo user and can't change anything."""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from officesentry.config import load_config
from officesentry.db import connect
from officesentry.web import create_app, public_demo

from .conftest import login, make_user

V2 = Path(__file__).resolve().parents[1]


def _mark_demo(db):
    db.execute("INSERT INTO settings (key, value) VALUES (?, '1')", (public_demo.MARKER,))


@pytest.fixture
def demo_app(cfg, db, two_tenants):
    cfg.public_demo = True
    cfg.public_demo_link = "https://officesentry.example"
    _mark_demo(db)
    make_user(db, public_demo.USERNAME, role="analyst", all_tenants=True)
    return create_app(cfg)


def _tables(db):
    names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    return {n: db.execute(f'SELECT * FROM "{n}"').fetchall() for n in names if n != "audit_log"}


def test_visitors_are_the_demo_user(demo_app):
    client = demo_app.test_client()
    resp = client.get("/")
    page = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Alpha Ltd" in page and "Bravo Inc" in page
    assert "You're in the demo." in page and "https://officesentry.example" in page
    assert "Demo visitor" in page and "Sign out" not in page
    assert resp.headers["X-Robots-Tag"] == "noindex, nofollow"
    # Nothing to sign in to, and no session row per visitor.
    assert client.get("/login").status_code == 302
    assert demo_app.extensions["officesentry.config"].public_demo
    db = connect(demo_app.extensions["officesentry.config"].db_path)
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


@pytest.mark.parametrize("case", ["setting off", "no marker", "admin", "disabled"])
def test_both_switches_and_a_non_admin_are_needed(cfg, db, two_tenants, case):
    cfg.public_demo = case != "setting off"
    if case != "no marker":
        _mark_demo(db)
    uid = make_user(db, public_demo.USERNAME, role="admin" if case == "admin" else "analyst", all_tenants=True)
    if case == "disabled":
        db.execute("UPDATE users SET disabled = 1 WHERE id = ?", (uid,))
    resp = create_app(cfg).test_client().get("/")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    # An admin or disabled "demo" still leaves the site read-only; it just signs nobody in.
    assert ("X-Robots-Tag" in resp.headers) == (case in ("admin", "disabled"))


def test_nothing_can_be_changed(demo_app, db):
    """Every route that takes a form or a fetch refuses, and no table changes."""
    client = demo_app.test_client()
    before = _tables(db)
    posts = [r for r in demo_app.url_map.iter_rules() if "POST" in r.methods]
    assert len(posts) > 50
    for rule in posts:
        url = re.sub(r"<(?:int:)?\w+>", "1", rule.rule)
        resp = client.post(url, data={"username": "x", "password": "y", "name": "Changed"},
                           headers={"Referer": "http://localhost/tenants/1"})
        assert resp.status_code == 302, rule.endpoint
        assert resp.headers["Location"] == "/tenants/1", rule.endpoint
    assert _tables(db) == before
    page = client.get("/tenants/1").get_data(as_text=True)
    assert "nothing can be changed here" in page


def test_fetches_get_the_notice_as_json(demo_app):
    resp = demo_app.test_client().post("/reports/mfa/views", headers={"X-Requested-With": "fetch"})
    assert resp.status_code == 403
    assert resp.get_json() == {"ok": False, "error": public_demo.NOTICE}


def test_refusal_never_redirects_off_site(demo_app):
    resp = demo_app.test_client().post("/tenants/1/run", headers={"Referer": "https://evil.example/x"})
    assert resp.status_code == 302 and resp.headers["Location"] == "/"


def test_banner_link_must_be_a_web_address(demo_app):
    demo_app.extensions["officesentry.config"].public_demo_link = "javascript:alert(1)"
    page = demo_app.test_client().get("/").get_data(as_text=True)
    assert "javascript:" not in page and "Get Office Sentry" not in page


def test_every_page_opens_for_the_demo_user(demo_app):
    client = demo_app.test_client()
    for rule in demo_app.url_map.iter_rules():
        if "GET" not in rule.methods or rule.arguments or rule.endpoint == "static":
            continue
        resp = client.get(rule.rule)
        # /health/ready answers 503 by design here: the demo has no worker.
        assert resp.status_code < 500 or rule.endpoint == "health_ready", rule.endpoint


def test_real_portals_are_unaffected(app, client, db, two_tenants):
    make_user(db, "admin")
    login(client, "admin")
    assert client.post("/tenants/1/pause").status_code == 302
    assert "You're in the demo" not in client.get("/").get_data(as_text=True)


def test_demo_data_public_makes_no_admin(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("demo_data", V2 / "scripts" / "demo_data.py")
    demo_data = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo_data)
    monkeypatch.setattr(sys, "argv", ["demo_data.py", "--data-dir", str(tmp_path), "--public"])
    demo_data.main()
    assert "Sign in as" not in capsys.readouterr().out
    db = connect(load_config(data_dir=tmp_path).db_path)
    assert [tuple(r) for r in db.execute("SELECT username, role FROM users")] == [("demo", "analyst")]
    assert db.execute("SELECT value FROM settings WHERE key = ?", (public_demo.MARKER,)).fetchone()[0] == "1"


def test_demo_container_has_no_way_out():
    # The demo's only network is internal (no internet), so it can't send mail, call Microsoft or post to a
    # webhook even if a request got past refuse_changes(). Checked as text: the tests don't depend on a YAML parser.
    compose = (V2 / "deploy" / "public" / "compose.yml").read_text()
    demo = compose.split("\n  demo:\n", 1)[1].split("\n\n", 1)[0]
    assert re.search(r"^    networks: \[public\]$", demo, re.M)
    assert "env_file" not in demo and "volumes" not in demo
    networks = compose.split("\nnetworks:\n", 1)[1]
    assert re.search(r"^  public:\n(?:    .*\n)*    internal: true$", networks, re.M)
