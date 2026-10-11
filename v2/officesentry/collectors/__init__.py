"""Collector registry. Importing this package registers every collector.

Import order is run order for scheduled collections: tenant_profile first
(licence capabilities), then the connection check (permissions and Global
Reader), then users (other collectors reuse that snapshot).
"""

from . import tenant_profile, connection, users  # noqa: F401, I001  (registration side effects, order matters)
from . import identity, per_user_mfa, secure_score  # noqa: F401
from . import auth_methods  # noqa: F401
from . import admin_activity  # noqa: F401
from . import apps, policies  # noqa: F401
from . import groups  # noqa: F401
from . import deleted_items  # noqa: F401
from . import exchange, mail_rules, mail_usage  # noqa: F401, I001  (these reuse the mailbox snapshot)
from . import mobile_devices, auto_replies, distribution_lists  # noqa: F401, I001  (these too)
from . import sign_ins, risky_users  # noqa: F401, I001
from . import domains  # noqa: F401
from . import devices  # noqa: F401
from . import directory_sync  # noqa: F401
from . import sharepoint  # noqa: F401  (shared files reuse the users and tenant profile snapshots)
from . import sharepoint_storage  # noqa: F401
from . import usage  # noqa: F401
from . import teams  # noqa: F401
from . import mail_flow  # noqa: F401
from . import app_sign_ins, group_nesting, user_details  # noqa: F401  (their reports join Apps, Groups and Users)
from . import admin_units  # noqa: F401  (its report joins Admin roles, Users and Groups)
from .base import REGISTRY, Collector, RunContext

__all__ = ["REGISTRY", "Collector", "RunContext"]
