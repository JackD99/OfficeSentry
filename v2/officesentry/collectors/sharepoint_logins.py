"""SharePoint login names (claims), as the Site access and Site permissions collectors read them.

Their own module so Site permissions doesn't import Site access: import order is run order, and Site access
reads the Site permissions snapshot, so Site permissions registers first.
"""

from __future__ import annotations

import re

# Login name shapes (claims). Anything else without "|" is a SharePoint group by its name.
_PERSON = re.compile(r"^i:0#\.f\|membership\|(.+)$", re.IGNORECASE)
_M365 = re.compile(r"^c:0o\.c\|federateddirectoryclaimprovider\|([^|]+?)(_o)?$", re.IGNORECASE)
_SECURITY = re.compile(r"^c:0t\.c\|tenant\|([^|]+)$", re.IGNORECASE)
EVERYONE = "c:0(.s|true"
EVERYONE_BUT_GUESTS = "spo-grid-all-users"
SYSTEM_LOGINS = ("app@sharepoint", "sharepoint\\system", "_spocache", "_spocrawler", "_spofrm")


def classify_login(login: str | None) -> tuple[str, str]:
    """``(kind, value)`` for a SharePoint login name.

    Kinds: ``person`` (address), ``guest`` (address), ``group_members`` and ``group_owners`` (Microsoft 365
    group id), ``security_group`` (id), ``everyone``, ``everyone_but_guests``, ``system``, ``sp_group``
    (the group's name) or ``unknown``.
    """
    login = (login or "").strip()
    low = login.lower()
    if not login:
        return ("unknown", "")
    if any(s in low for s in SYSTEM_LOGINS):
        return ("system", login)
    if low == EVERYONE:
        return ("everyone", login)
    if EVERYONE_BUT_GUESTS in low:
        return ("everyone_but_guests", login)
    if m := _PERSON.match(login):
        who = m.group(1)
        return ("guest" if "#ext#" in who.lower() or "urn:spo:guest" in who.lower() else "person", who.lower())
    if "urn:spo:guest" in low:
        return ("guest", login.rsplit("|", 1)[-1].lower())
    if m := _M365.match(login):
        return ("group_owners" if m.group(2) else "group_members", m.group(1).lower())
    if m := _SECURITY.match(login):
        return ("security_group", m.group(1).lower())
    if "|" not in login:
        return ("sp_group", login)
    return ("unknown", login)


def guest_address(login_value: str, email: str | None) -> str:
    """A guest's own address: the invitation email, else the address folded into the login name."""
    if email and "@" in email:
        return email.lower()
    local = login_value.split("#ext#", 1)[0]
    if "_" in local and "@" not in local:
        # Microsoft turns the "@" into the last underscore (a domain has none): jo_smith_supplier.com.
        name, domain = local.rsplit("_", 1)
        local = f"{name}@{domain}"
    return local.lower()
