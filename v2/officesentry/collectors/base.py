"""Collector interface and registry.

A collector reads one area of a tenant and writes a snapshot: objects (one per
user, role, policy...) and metrics (numbers for trends). Collectors never
write to the tenant and never render anything; reports read the snapshots.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar

from .. import store
from ..auditlog.feed import FeedClient
from ..autodesk.api import AutodeskClient
from ..db import transaction
from ..exchange import ExchangeClient
from ..graph import GraphClient
from ..text import plural

REGISTRY: dict[str, type[Collector]] = {}


class NothingToRead(Exception):
    """A collector that reads what others collected found none of it (say sign-in has never worked), so it
    asked Microsoft nothing. The run fails with this reason rather than succeeding with nothing in it."""


def register(cls: type[Collector]) -> type[Collector]:
    if cls.key in REGISTRY:
        raise ValueError(f"duplicate collector key {cls.key!r}")
    REGISTRY[cls.key] = cls
    return cls


def core() -> list[str]:
    """Every data source collected for every client, in run order: all but the add-ons'."""
    return [key for key, cls in REGISTRY.items() if not cls.addon]


def for_tenant(conn, tenant) -> list[str]:
    """The data sources collected for ``tenant``: every core one, and each add-on's where it's on."""
    return [key for key, cls in REGISTRY.items() if not cls.addon or cls.applies(conn, tenant)]


@dataclass
class RunContext:
    conn: object
    run_id: int
    tenant: dict
    graph: GraphClient
    capabilities: dict = field(default_factory=dict)
    client_id: str | None = None  # the app (client) ID Office Sentry signs in to this tenant as
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False
    exchange_factory: Callable[[], ExchangeClient] | None = None
    _exchange: ExchangeClient | None = None
    feed_factory: Callable[[], FeedClient] | None = None
    _feed: FeedClient | None = None
    autodesk_factory: Callable[[object], AutodeskClient] | None = None

    @property
    def exchange(self) -> ExchangeClient:
        """Exchange admin API client, created on first use (most collectors don't need it)."""
        if self._exchange is None:
            if self.exchange_factory is None:
                raise RuntimeError("no Exchange client configured for this run")
            self._exchange = self.exchange_factory()
        return self._exchange

    @property
    def feed(self) -> FeedClient:
        """The audit log add-on's feed client (``auditlog/feed.py``), signed in as the add-on's own app, created
        on first use. Only the add-on's collector uses it."""
        if self._feed is None:
            if self.feed_factory is None:
                raise RuntimeError("no audit feed client configured for this run")
            self._feed = self.feed_factory()
        return self._feed

    def autodesk(self, sign_in) -> AutodeskClient:
        """An Autodesk client for one Autodesk sign-in (``autodesk/setup.py``). Only the Autodesk collector uses it."""
        if self.autodesk_factory is None:
            raise RuntimeError("no Autodesk client configured for this run")
        return self.autodesk_factory(sign_in)

    def log(self, message: str, level: str = "info") -> None:
        store.log_event(self.conn, self.run_id, level, message)

    def warn(self, message: str) -> None:
        """Record a section that could not be collected; the run becomes 'partial'."""
        self.warnings.append(message)
        self.log(message, "warning")

    def objects(self, kind: str, items: list[tuple[str, dict]]) -> None:
        with transaction(self.conn):
            store.add_objects(self.conn, self.run_id, kind, items)
        self.log(f"Stored {plural(len(items), kind + ' object')}")

    def metric(self, key: str, value: float) -> None:
        store.add_metric(self.conn, self.run_id, self.tenant["id"], key, value)

    def paged(self, result) -> list:
        if result.truncated:
            self.truncated = True
            self.warn("Result was truncated by the page limit")
        return result.items

    def note_exchange_cuts(self) -> None:
        """After the collector ran: a warning for each cmdlet whose answer the Exchange client cut at its
        page cap (``ExchangeClient.truncated``), so the run is partial and says what's missing."""
        if self._exchange is None:
            return
        for cmdlet, pages in self._exchange.truncated.items():
            self.truncated = True
            self.warn(f"{cmdlet} returned more than {pages} pages of results; the rest were left out")
        self._exchange.truncated.clear()

    def sign_in_failure(self) -> Exception | None:
        """Why signing in to Graph or Exchange failed during the run, if it did. A collector that reads item by
        item counts each refused request as one item it couldn't read and carries on, so without this a run
        that couldn't sign in at all would finish as partial with nothing read, and its checks pass."""
        return getattr(self.graph, "sign_in_error", None) or getattr(self._exchange, "sign_in_error", None)


class Collector:
    key: ClassVar[str]
    title: ClassVar[str]
    description: ClassVar[str] = ""
    nightly: ClassVar[bool] = True
    # Graph application permissions this collector needs (documentation + UI).
    permissions: ClassVar[tuple[str, ...]] = ()
    # Licence capabilities (``capabilities.FLAGS``) this collector can't read anything without, and those
    # that only fill in part of what it reads. Reports and pages say which licence a client is missing from
    # these (``capabilities.report_needs``), so a collector that skips an area on a capability declares it.
    needs: ClassVar[tuple[str, ...]] = ()
    more_with: ClassVar[tuple[str, ...]] = ()
    # An add-on's data source (the audit log add-on's): collected only for the clients ``applies`` names, and
    # never by "collect everything", a client's first collection or the freshness checks.
    addon: ClassVar[bool] = False

    @classmethod
    def applies(cls, conn, tenant) -> bool:
        """Whether this add-on's data source is collected for ``tenant`` now."""
        return not cls.addon

    def collect(self, ctx: RunContext) -> None:
        raise NotImplementedError


GraphFactory = Callable[[dict, dict], GraphClient]
ExchangeFactory = Callable[[dict, dict], ExchangeClient]
