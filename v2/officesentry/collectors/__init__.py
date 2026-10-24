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
from . import security_alerts  # noqa: F401
from . import domains  # noqa: F401
from . import devices  # noqa: F401
from . import intune_policies  # noqa: F401  (its report counts the devices snapshot by platform)
from . import detected_apps  # noqa: F401
from . import directory_sync  # noqa: F401
from . import sharepoint  # noqa: F401  (shared files reuse the users and tenant profile snapshots)
from . import sharepoint_storage  # noqa: F401
from . import site_permissions  # noqa: F401  (reuses the site list)
from . import site_access  # noqa: F401  (reuses the site list and the groups, users and site permissions snapshots)
from . import usage  # noqa: F401
from . import teams  # noqa: F401
from . import mail_flow  # noqa: F401
from . import mail_protection  # noqa: F401  (its report tells outside addresses by the mailbox snapshot's domains)
from . import app_sign_ins, group_nesting, user_details  # noqa: F401  (their reports join Apps, Groups and Users)
from . import admin_units  # noqa: F401  (its report joins Admin roles, Users and Groups)
from . import service_health  # noqa: F401
from . import agents  # noqa: F401  (its report joins Apps and consents and Admin roles)
from . import audit_activity  # noqa: F401  (the audit log add-on's; only for clients it's on for)
from .base import REGISTRY, Collector, RunContext, core, for_tenant

__all__ = ["REGISTRY", "Collector", "RunContext", "core", "for_tenant"]
