"""`python -m officesentry serve`: waitress on Windows, gunicorn elsewhere."""

import sys
import types

import pytest

from officesentry.__main__ import main


@pytest.fixture
def env(cfg, monkeypatch):
    monkeypatch.setenv("OFFICESENTRY_DATA_DIR", str(cfg.data_dir))
    return cfg


def test_serve_uses_waitress_on_windows(env, monkeypatch):
    calls = []
    fake = types.ModuleType("waitress")
    fake.serve = lambda app, **kw: calls.append((app, kw))
    monkeypatch.setitem(sys.modules, "waitress", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    main(["serve", "--port", "8123", "--threads", "6"])
    [(app, kw)] = calls
    assert kw == {"host": "127.0.0.1", "port": 8123, "threads": 6}
    assert app.test_client().get("/health").json["ok"] is True


@pytest.mark.skipif(sys.platform == "win32", reason="gunicorn doesn't run on Windows")
def test_serve_uses_gunicorn_elsewhere(env, monkeypatch):
    from gunicorn.app.base import BaseApplication

    seen = {}

    def run(self):
        seen.update(bind=self.cfg.bind, workers=self.cfg.workers, threads=self.cfg.threads, app=self.load())

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(BaseApplication, "run", run)
    main(["serve", "--host", "0.0.0.0", "--port", "8124"])  # noqa: S104
    assert seen["bind"] == ["0.0.0.0:8124"] and seen["workers"] == 2 and seen["threads"] == 4
    # The app was built (and the database upgraded) before any worker starts.
    assert seen["app"].test_client().get("/health").json["ok"] is True
