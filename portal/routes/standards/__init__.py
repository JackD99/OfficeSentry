"""
portal/routes/standards — Standards, BPA, Alerts, Tenant Groups, and Secure Score.

Split into sub-modules for maintainability. All routes register on the shared
``bp`` Blueprint which is re-exported here for ``app.py``.

Public API (used by app.py and tasks.py):
    bp, process_alert_rules, BPA_CHECKS
"""

import logging

from flask import Blueprint

bp = Blueprint("standards", __name__)

log = logging.getLogger("portal")

# Import definitions first (no routes, just data)
from routes.standards.definitions import (  # noqa: E402, F401
    BPA_CHECKS, IMPACT_COLORS, GROUP_COLORS,
)

# Import sub-modules to register their routes on ``bp``
from routes.standards import groups, templates, bpa, scores, alerts  # noqa: E402, F401

# Re-export for tasks.py
from routes.standards.alerts import process_alert_rules  # noqa: E402, F401
