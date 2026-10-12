"""Office Sentry's own log: where it goes and what each line carries.

Every process logs to standard error as before (Docker and systemd collect that). The portal and the worker
also keep a short rolling copy in ``<data dir>/logs/<process>.log`` (``LOG_FILES`` files of ``LOG_BYTES``
each, so a few megabytes at most), which is what the support package (``support.py``) picks up: by the time
someone asks for help, the container's log has usually scrolled past the moment that mattered.

Lines carry the context they happened in, set with ``context(...)``: the request's id (``req=…``, also sent
back as the ``X-Request-ID`` header and shown on the error page as the reference to quote), who was signed
in, and the tenant, run or task the worker was on. So an error in the log can be matched to the page or
the run it came from without guessing.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import secrets
import time
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s %(name)s%(ctx)s: %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
LOG_BYTES = 2 * 1024 * 1024
LOG_FILES = 3  # the current file and two before it
PROCESSES = ("web", "worker")
# The first two fields of a line, as FORMAT writes them: when, and how bad.
LINE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) (\w+) ")
# extra={IN_FILE: "..."}: what the log files say instead of the message, which only standard error shows
# (the setup link's token stays out of the files the support package and the Support page read).
IN_FILE = "in_file"

_context: ContextVar[dict | None] = ContextVar("officesentry_log_context", default=None)
_installed: dict[str, logging.Handler] = {}


def new_request_id() -> str:
    return secrets.token_hex(6)


@contextlib.contextmanager
def context(**fields) -> Iterator[None]:
    """Add ``fields`` (``req``, ``user``, ``tenant``, ``run``, ``task``...) to every line logged inside."""
    current = _context.get() or {}
    token = _context.set({**current, **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _context.reset(token)


def push(**fields):
    """``context()`` for code that can't use ``with`` (a web request: set before, reset at teardown)."""
    current = _context.get() or {}
    return _context.set({**current, **{k: v for k, v in fields.items() if v is not None}})


def pop(token) -> None:
    if token is None:
        return
    try:
        _context.reset(token)
    except (ValueError, RuntimeError):  # set in another context: just clear
        _context.set(None)


def current_context() -> dict:
    return dict(_context.get() or {})


class ContextFilter(logging.Filter):
    """Puts the context on the record as ``ctx`` (" req=… tenant=…", or "") for ``FORMAT``."""

    def filter(self, record: logging.LogRecord) -> bool:
        fields = _context.get() or {}
        record.ctx = "".join(f" {k}={v}" for k, v in fields.items()) if fields else ""
        return True


class Formatter(logging.Formatter):
    """UTC times, in the same ISO form as everything Office Sentry stores."""

    converter = staticmethod(time.gmtime)

    def __init__(self) -> None:
        super().__init__(FORMAT, DATE_FORMAT)


class SharedRotatingFileHandler(RotatingFileHandler):
    """A rotating log file that several processes may write (gunicorn's workers share ``web.log``).

    Before each line it checks the file is still the one it opened (another process may have rotated it)
    and reopens the current one if not, as ``WatchedFileHandler`` does; a rotation that fails because another
    process holds the file (Windows) is skipped rather than raised.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(path, maxBytes=LOG_BYTES, backupCount=LOG_FILES - 1, encoding="utf-8", delay=True)
        self._ident: tuple[int, int] | None = None

    def _stat(self) -> tuple[int, int] | None:
        try:
            st = os.stat(self.baseFilename)
        except OSError:
            return None
        return st.st_dev, st.st_ino

    def emit(self, record: logging.LogRecord) -> None:
        if (instead := getattr(record, IN_FILE, None)) is not None:
            record = logging.makeLogRecord({**record.__dict__, "msg": instead, "args": ()})
        if self.stream is not None and self._stat() != self._ident:
            self.stream.close()
            self.stream = None  # reopened by the next write, on the file now at that name
        super().emit(record)
        if self.stream is not None and self._ident is None:
            self._ident = self._stat()

    def doRollover(self) -> None:  # noqa: N802 (logging's name)
        try:
            super().doRollover()
        except OSError:
            self.stream = None
        self._ident = None


def log_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "logs"


def log_file(data_dir: Path, process: str) -> Path:
    return log_dir(data_dir) / f"{process}.log"


def setup(data_dir: Path | None, process: str) -> None:
    """Log to standard error, and (for ``web`` and ``worker``) to the rolling file in the data folder.

    Safe to call again: a second call for the same process replaces its file handler (tests build many
    apps), and a folder that can't be written only loses the file, never the process.
    """
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    for handler in root.handlers:
        _decorate(handler)
    if old := _installed.pop(process, None):
        root.removeHandler(old)
        old.close()
    if data_dir is None or process not in PROCESSES:
        return
    try:
        folder = log_dir(data_dir)
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        handler = SharedRotatingFileHandler(log_file(data_dir, process))
    except OSError as exc:
        root.warning("Not keeping a log file in %s: %s", log_dir(data_dir), exc.strerror or exc)
        return
    _decorate(handler)
    root.addHandler(handler)
    _installed[process] = handler


def _decorate(handler: logging.Handler) -> None:
    if not any(isinstance(f, ContextFilter) for f in handler.filters):
        handler.addFilter(ContextFilter())
    if not isinstance(handler.formatter, Formatter):
        handler.setFormatter(Formatter())


# -- Reading the files back (the support package and the Support page) -------------------------------------


def files(data_dir: Path, process: str) -> list[Path]:
    """The process's log files, oldest first, that exist."""
    base = log_file(data_dir, process)
    found = [base.with_name(f"{base.name}.{n}") for n in range(LOG_FILES - 1, 0, -1)] + [base]
    return [p for p in found if p.exists()]


def tail(data_dir: Path, process: str, max_bytes: int) -> str:
    """The last ``max_bytes`` of the process's log, across its rotated files, starting on a whole line."""
    chunks: list[bytes] = []
    left = max_bytes
    for path in reversed(files(data_dir, process)):
        if left <= 0:
            break
        try:
            size = path.stat().st_size
            with path.open("rb") as fh:
                if size > left:
                    fh.seek(size - left)
                data = fh.read(left)
        except OSError:
            continue
        chunks.insert(0, data)
        left -= len(data)
    text = b"".join(chunks).decode("utf-8", "replace")
    if left <= 0 and "\n" in text:
        text = text[text.index("\n") + 1 :]
    return text


def recent_errors(data_dir: Path, hours: int = 24, now: datetime | None = None) -> dict[str, int]:
    """How many ERROR (or worse) lines each process logged in the last ``hours``: {"web": 2, "worker": 0}."""
    since = ((now or datetime.now(UTC)) - timedelta(hours=hours)).strftime(DATE_FORMAT)
    counts = {}
    for process in PROCESSES:
        n = 0
        for path in files(data_dir, process):
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        m = LINE.match(line)
                        if m and m.group(1) >= since and m.group(2) in ("ERROR", "CRITICAL"):
                            n += 1
            except OSError:
                continue
        counts[process] = n
    return counts


def sizes(data_dir: Path) -> dict[str, int]:
    """Bytes of log kept per process."""
    return {p: sum(f.stat().st_size for f in files(data_dir, p)) for p in PROCESSES}
