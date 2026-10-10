"""One page per account: everything Office Sentry has collected about one person.

Licences, MFA, admin roles, groups, mailbox and who can get into it, devices,
OneDrive sharing, apps, sign-ins and admin changes, open findings and what
changed for the account across stored snapshots. Like the detail reports it
reads stored data only and returns plain data; ``user.html`` and the exports
format it.

Each area of the page is a "part" in its own module: a function decorated
with ``@part(order)`` (``base.py``) that returns a ``Part`` (or None to leave
the area out) and can raise items for "Needs attention" with
``person.flag()``. Orders are spaced by 10 so a new area slots in between
without touching the others; import its module here. ``page.build()`` puts
the page together, ``directory()`` is the Users tab and ``leaver_sheet()``
the leaver checklist.
"""

# Part modules register themselves with @part on import; the page sorts them by order.
from . import (  # noqa: F401
    apps,
    devices,
    groups,
    leavers,
    licences,
    mailbox,
    mfa,
    onedrive,
    roles,
    sign_ins,
    timeline,
    usage,
)
from .base import PARTS, Attention, Event, Part, Person, Profile, part  # noqa: F401
from .directory import DirectoryRow, directory  # noqa: F401
from .history import history  # noqa: F401
from .leavers import LeaverSheet, Reclaim, leaver_sheet, reclaim  # noqa: F401
from .page import build, find_user  # noqa: F401
from .timeline import TIMELINE_DAYS, TIMELINE_LIMIT  # noqa: F401

# Every module of the package, for ``user_profile``.
MODULES = ("base", "page", "history", "directory", "apps", "devices", "groups", "leavers", "licences", "mailbox", "mfa",
           "onedrive", "roles", "sign_ins", "timeline", "usage")
