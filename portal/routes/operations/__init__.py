"""
portal/routes/operations — MSP bulk operations for user, mailbox, Teams,
Exchange, and SharePoint management.

Split into sub-modules for maintainability. All routes register on the shared
``bp`` Blueprint which is re-exported here for ``app.py``.

Public API (used by tests and app.py):
    bp, OPERATIONS, CATEGORIES
"""

import logging

from flask import Blueprint

bp = Blueprint("operations", __name__)

log = logging.getLogger("portal")

# Import definitions first (no routes, just data)
from routes.operations.definitions import OPERATIONS, CATEGORIES  # noqa: E402, F401

# Import sub-modules to register their routes on ``bp``
from routes.operations import commands, routes  # noqa: E402, F401
