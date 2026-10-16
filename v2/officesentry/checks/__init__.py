"""Checks turn a tenant snapshot into findings.

``base`` has the framework (``@rule``, ``RULES``, ``evaluate()``, ``Finding``,
``RuleResult``); every other module is one area's checks, registered into
``RULES`` when it's imported here. Checks are listed in the order of these
imports, so a new area goes at the end. Checks read stored snapshots only,
never Graph.
"""

from .base import RULES, SEVERITIES, SEVERITY_RANK, Finding, Rule, RuleResult, evaluate, rule  # noqa: F401

# isort: off
from . import identity, email, apps, tenant_settings, sharing, domains, groups, devices, hybrid
from . import admin_activity, sharepoint, auth_methods, licences, sign_ins, risky_users, accounts, storage
from . import security_alerts
# isort: on

AREAS = (
    identity,
    email,
    apps,
    tenant_settings,
    sharing,
    domains,
    groups,
    devices,
    hybrid,
    admin_activity,
    sharepoint,
    auth_methods,
    licences,
    sign_ins,
    risky_users,
    accounts,
    storage,
    security_alerts,
)
