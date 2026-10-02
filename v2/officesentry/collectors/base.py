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
from ..db import transaction
from ..graph import GraphClient

REGISTRY: dict[str, type[Collector]] = {}


def register(cls: type[Collector]) -> type[Collector]:
    if cls.key in REGISTRY:
        raise ValueError(f"duplicate collector key {cls.key!r}")
    REGISTRY[cls.key] = cls
    return cls


@dataclass
class RunContext:
    conn: object
    run_id: int
    tenant: dict
    graph: GraphClient
    capabilities: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False

    def log(self, message: str, level: str = "info") -> None:
        store.log_event(self.conn, self.run_id, level, message)

    def warn(self, message: str) -> None:
        """Record a section that could not be collected; the run becomes 'partial'."""
        self.warnings.append(message)
        self.log(message, "warning")

    def objects(self, kind: str, items: list[tuple[str, dict]]) -> None:
        with transaction(self.conn):
            store.add_objects(self.conn, self.run_id, kind, items)
        self.log(f"Stored {len(items)} {kind} object(s)")

    def metric(self, key: str, value: float) -> None:
        store.add_metric(self.conn, self.run_id, self.tenant["id"], key, value)

    def paged(self, result) -> list:
        if result.truncated:
            self.truncated = True
            self.warn("Result was truncated by the page limit")
        return result.items


class Collector:
    key: ClassVar[str]
    title: ClassVar[str]
    description: ClassVar[str] = ""
    nightly: ClassVar[bool] = True
    # Graph application permissions this collector needs (documentation + UI).
    permissions: ClassVar[tuple[str, ...]] = ()

    def collect(self, ctx: RunContext) -> None:
        raise NotImplementedError


GraphFactory = Callable[[dict, dict], GraphClient]
