"""The portal on a production web server, for running without Docker.

``python -m officesentry serve`` uses waitress on Windows (gunicorn doesn't
run there) and gunicorn everywhere else, with the same settings as the
Docker image. The app is built once, before serving, so the database is
upgraded once and gunicorn's workers never race to do it.
"""

from __future__ import annotations

import sys

from .config import Config

# The path only, never the query string (search terms, filter values, the setup link) or the referer, which
# carry what people looked at. The Dockerfile's gunicorn command uses the same.
ACCESS_LOG_FORMAT = '%(h)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s %(M)sms "%(a)s"'


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8000, threads: int = 8, workers: int = 2) -> None:
    from .web import create_app

    app = create_app(cfg)
    if sys.platform == "win32":
        import waitress

        # One process with a thread pool: waitress has no worker processes.
        waitress.serve(app, host=host, port=port, threads=threads)
        return

    from gunicorn.app.base import BaseApplication

    options = {
        "bind": f"{host}:{port}",
        "workers": workers,
        "threads": max(1, threads // workers),
        "timeout": 120,
        "accesslog": "-",
        "access_log_format": ACCESS_LOG_FORMAT,
    }

    class Gunicorn(BaseApplication):
        def load_config(self):
            for key, value in options.items():
                self.cfg.set(key, value)

        def load(self):
            return app

    Gunicorn().run()
