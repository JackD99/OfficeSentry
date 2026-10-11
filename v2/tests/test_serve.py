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
        seen.update(
            bind=self.cfg.bind,
            workers=self.cfg.workers,
            threads=self.cfg.threads,
            app=self.load(),
            log_format=self.cfg.access_log_format,
        )

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(BaseApplication, "run", run)
    main(["serve", "--host", "0.0.0.0", "--port", "8124"])  # noqa: S104
    assert seen["bind"] == ["0.0.0.0:8124"] and seen["workers"] == 2 and seen["threads"] == 4
    # The app was built (and the database upgraded) before any worker starts.
    assert seen["app"].test_client().get("/health").json["ok"] is True
    # The access log keeps the path, never the query string or the referer.
    assert "%(U)s" in seen["log_format"] and "%(r)s" not in seen["log_format"] and "%(f)s" not in seen["log_format"]


def test_docker_access_log_leaves_out_the_query_string():
    from pathlib import Path

    from officesentry.serve import ACCESS_LOG_FORMAT

    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    assert '"--access-logformat", "' + ACCESS_LOG_FORMAT.replace('"', '\\"') + '"' in dockerfile
