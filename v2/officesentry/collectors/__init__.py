"""Collector registry. Importing this package registers every collector.

Import order is run order for scheduled collections: tenant_profile first
(licence capabilities), then users (other collectors reuse that snapshot).
"""

from . import tenant_profile, users  # noqa: F401, I001  (registration side effects, order matters)
from . import identity, secure_score  # noqa: F401
from .base import REGISTRY, Collector, RunContext

__all__ = ["REGISTRY", "Collector", "RunContext"]
