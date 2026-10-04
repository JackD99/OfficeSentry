"""Collector registry. Importing this package registers every collector.

Import order is run order for scheduled collections: tenant_profile first
(licence capabilities), then users (other collectors reuse that snapshot).
"""

from . import tenant_profile, users  # noqa: F401, I001  (registration side effects, order matters)
from . import identity, per_user_mfa, secure_score  # noqa: F401
from . import admin_activity  # noqa: F401
from . import apps, policies  # noqa: F401
from . import groups  # noqa: F401
from . import exchange, mail_rules, mail_usage  # noqa: F401, I001  (these reuse the mailbox snapshot)
from . import sign_ins  # noqa: F401
from . import domains  # noqa: F401
from . import devices  # noqa: F401
from . import directory_sync  # noqa: F401
from . import sharepoint  # noqa: F401  (shared files reuse the users and tenant profile snapshots)
from . import sharepoint_storage  # noqa: F401
from . import usage  # noqa: F401
from .base import REGISTRY, Collector, RunContext

__all__ = ["REGISTRY", "Collector", "RunContext"]
