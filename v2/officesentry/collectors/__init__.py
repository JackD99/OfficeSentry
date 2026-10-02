"""Collector registry. Importing this package registers every collector."""

from . import tenant_profile, users  # noqa: F401  (registration side effects)
from .base import REGISTRY, Collector, RunContext

__all__ = ["REGISTRY", "Collector", "RunContext"]
