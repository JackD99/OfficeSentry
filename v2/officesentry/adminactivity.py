"""Admin activity: which Entra ID directory audit events matter, and what they mean.

Shared by the admin activity collector, its report and the federation check.
The directory audit log records thousands of routine events (service
principals updated by Microsoft, licence changes...). Only the activities in
``ACTIVITIES`` are kept: the ones an MSP would want to know someone did.
"""

from __future__ import annotations

import json
import re

from .devices import parse_ts

WINDOW_DAYS = 30        # how far back we ask for
FREE_RETENTION_DAYS = 7  # what Entra ID keeps without P1
MAX_TARGETS = 5

# Kinds of change, and the area each belongs to in the report.
AREAS = {
    "role_grant": "Admin roles", "role_removal": "Admin roles", "role_activation": "Admin roles",
    "ca": "Conditional Access", "auth_policy": "Tenant policies",
    "consent": "Apps", "app_role": "Apps", "app_credential": "Apps", "app_owner": "Apps",
    "federation": "Domains", "domain": "Domains",
    "password_reset": "Accounts", "user_delete": "Accounts", "user_restore": "Accounts",
    "security_info": "Accounts", "partner": "Partners",
}

# (category, activityDisplayName) -> kind. Names are compared after normalising (see _norm),
# because Microsoft writes some with an en dash and a trailing space.
_ACTIVITIES = {
    "RoleManagement": {
        "role_grant": (
            "Add member to role", "Add eligible member to role", "Add scoped member to role",
            "Add member to role in PIM completed (permanent)", "Add member to role in PIM completed (timebound)",
            "Add eligible member to role in PIM completed (permanent)",
            "Add eligible member to role in PIM completed (timebound)",
            "Add member to role outside of PIM (permanent)",
        ),
        "role_removal": (
            "Remove member from role", "Remove eligible member from role", "Remove scoped member from role",
            "Remove member from role in PIM completed (permanent)",
            "Remove member from role in PIM completed (timebound)",
            "Remove eligible member from role in PIM completed (permanent)",
            "Remove eligible member from role in PIM completed (timebound)",
            "Remove permanent direct role assignment", "Remove permanent eligible role assignment",
        ),
        "role_activation": ("Add member to role completed (PIM activation)",),
    },
    "Policy": {
        "ca": (
            "Add conditional access policy", "Update conditional access policy", "Delete conditional access policy",
            "Add named location", "Update named location", "Delete named location",
        ),
        "auth_policy": (
            "Update authorization policy", "Authentication Methods Policy Update",
            "Update authentication methods policy",
        ),
    },
    "ApplicationManagement": {
        "consent": ("Consent to application", "Add delegated permission grant"),
        "app_role": ("Add app role assignment to service principal",),
        "app_credential": (
            "Add service principal credentials", "Remove service principal credentials",
            "Update application - Certificates and secrets management",
        ),
        "app_owner": ("Add owner to application", "Add owner to service principal"),
    },
    "DirectoryManagement": {
        "federation": ("Set domain authentication", "Set federation settings on domain"),
        "domain": (
            "Add unverified domain", "Add verified domain", "Verify domain", "Verify email verified domain",
            "Remove unverified domain", "Remove verified domain", "Delete domain",
        ),
        "partner": ("Add partner to company", "Remove partner from company"),
    },
    "UserManagement": {
        "password_reset": ("Reset user password", "Reset password (by admin)"),
        "user_delete": ("Delete user", "Hard delete user"),
        "user_restore": ("Restore user",),
        "security_info": (
            "Admin registered security info", "Admin updated security info", "Admin deleted security info",
            "Admin registered temporary access pass method for user",
        ),
    },
}


def _norm(name: str | None) -> str:
    return re.sub(r"\s+", " ", (name or "").replace("–", "-").replace("—", "-")).strip().lower()


ACTIVITIES: dict[tuple[str, str], str] = {
    (category.lower(), _norm(name)): kind
    for category, kinds in _ACTIVITIES.items() for kind, names in kinds.items() for name in names
}

CA_STATES = {"enabled": "On", "disabled": "Off", "enabledForReportingButNotEnforced": "Report-only"}
KEY_TYPES = {"Password": "secret", "AsymmetricX509Cert": "certificate", "Symmetric": "symmetric key"}


def kind_of(event: dict) -> str | None:
    """The kind of security-relevant change, or None for an event we don't keep."""
    return ACTIVITIES.get(((event.get("category") or "").lower(), _norm(event.get("activityDisplayName"))))


def _value(raw):
    """Modified property values are JSON-encoded strings; decode them when possible."""
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _props(event: dict) -> dict[str, tuple]:
    props = {}
    for target in event.get("targetResources") or []:
        for p in (target or {}).get("modifiedProperties") or []:
            if p.get("displayName") and p["displayName"] not in props:
                props[p["displayName"]] = (_value(p.get("oldValue")), _value(p.get("newValue")))
    return props


def _actor(event: dict) -> tuple[str, str, str]:
    """(who, 'user' or 'app', IP address)."""
    by = event.get("initiatedBy") or {}
    user, app = by.get("user") or {}, by.get("app") or {}
    if user.get("userPrincipalName") or user.get("displayName"):
        return user.get("userPrincipalName") or user["displayName"], "user", user.get("ipAddress") or ""
    if app.get("displayName") or app.get("servicePrincipalName") or app.get("appId"):
        return app.get("displayName") or app.get("servicePrincipalName") or app["appId"], "app", ""
    return "Unknown", "", user.get("ipAddress") or ""


def _keys(value) -> dict[str, str]:
    """KeyIdentifier -> KeyType from an app's KeyDescription property."""
    text = json.dumps(value) if not isinstance(value, str) else value
    return dict(re.findall(r"KeyIdentifier=([^,\]\"]+),KeyType=([^,\]\"]+)", text or ""))


def normalise(event: dict) -> dict:
    """A compact, stored form of one directory audit event."""
    kind = kind_of(event) or ""
    props = _props(event)
    actor, actor_type, ip = _actor(event)
    targets = [t for t in event.get("targetResources") or [] if t]
    old_role, new_role = props.get("Role.DisplayName") or (None, None)
    role = new_role or old_role or next(
        (t.get("displayName") for t in targets if t.get("type") == "Role" and t.get("displayName")), None)
    labels = [t.get("userPrincipalName") or t.get("displayName") for t in targets if t.get("type") != "Role"]
    detail, credential_added, ca_disabled = "", False, False

    if kind.startswith("role_"):
        detail = role or ""
    elif kind == "app_role":
        grantee = (props.get("ServicePrincipal.DisplayName") or (None, None))[1]
        permission = (props.get("AppRole.Value") or (None, None))[1]
        resource = targets[0].get("displayName") if targets else None
        labels = [grantee] if grantee else labels[:1]
        detail = " on ".join(p for p in (permission, resource) if p)
    elif kind == "consent":
        detail = _consent_detail(props)
    elif kind == "app_credential":
        detail, credential_added = _credential_detail(event, props)
    elif kind == "ca" and "ConditionalAccessPolicy" in props:
        old, new = (v if isinstance(v, dict) else {} for v in props["ConditionalAccessPolicy"])
        before, after = old.get("state"), new.get("state")
        if before and after and before != after:
            detail = f"{CA_STATES.get(before, before)} → {CA_STATES.get(after, after)}"
            ca_disabled = before == "enabled"
        elif after or before:
            detail = CA_STATES.get(after or before, after or before)
        if not any(labels):
            labels = [new.get("displayName") or old.get("displayName")]

    labels = [str(t) for t in labels if t]
    return {
        "time": event.get("activityDateTime"),
        "activity": re.sub(r"\s+", " ", event.get("activityDisplayName") or "").strip(),
        "category": event.get("category"),
        "kind": kind,
        "area": AREAS.get(kind, ""),
        "result": event.get("result") or "",
        "reason": (event.get("resultReason") or "")[:200],
        "actor": actor,
        "actor_type": actor_type,
        "ip": ip,
        "targets": labels[:MAX_TARGETS],
        "more_targets": max(len(labels) - MAX_TARGETS, 0),
        "detail": str(detail)[:300],
        "credential_added": credential_added,
        "ca_disabled": ca_disabled,
    }


def _consent_detail(props: dict) -> str:
    perms = str((props.get("ConsentAction.Permissions") or (None, ""))[1] or "")
    scopes = sorted({s for part in re.findall(r"Scope: ([^,\]]+)", perms.split("=>")[-1]) for s in part.split()})
    everyone = str((props.get("ConsentContext.OnBehalfOfAll") or (None, ""))[1]).lower() == "true"
    admin = str((props.get("ConsentContext.IsAdminConsent") or (None, ""))[1]).lower() == "true"
    who = "Admin consent for everyone" if admin and everyone else ("Admin consent" if admin else "User consent")
    return f"{who}: {' '.join(scopes)}" if scopes else who


def _credential_detail(event: dict, props: dict) -> tuple[str, bool]:
    """(what changed, whether a credential was added)."""
    if "KeyDescription" in props:
        old, new = (_keys(v) for v in props["KeyDescription"])
        added = [KEY_TYPES.get(t, t) for k, t in new.items() if k not in old]
        removed = [KEY_TYPES.get(t, t) for k, t in old.items() if k not in new]
        if added or removed:
            parts = ([f"Added {', '.join(added)}"] if added else []) + \
                ([f"Removed {', '.join(removed)}"] if removed else [])
            return "; ".join(parts), bool(added)
    name = _norm(event.get("activityDisplayName"))
    if name.startswith("remove "):
        return "Credential removed", False
    # Can't tell what changed: treat it as an addition, so it is reviewed rather than missed.
    return ("Credential added" if name.startswith("add ") else "Credentials changed"), True


def review(event: dict) -> tuple[str, str] | None:
    """(severity, why) for a successful change worth a second look, else None."""
    if event.get("result") != "success":
        return None
    kind = event.get("kind")
    if kind == "federation":
        return "high", "Changes how users of a domain sign in; a rogue federation lets someone sign in as anyone"
    if kind == "app_credential" and event.get("credential_added"):
        return "medium", "New secret or certificate: whoever holds it can sign in as the app"
    if kind == "role_grant":
        return "medium", "Admin role granted"
    if kind == "ca" and _norm(event.get("activity")).startswith("delete conditional access"):
        return "medium", "Conditional Access policy deleted"
    if kind == "ca" and event.get("ca_disabled"):
        return "medium", "Conditional Access policy no longer enforced"
    return None


def covered_days(window: dict | None) -> int:
    """Days of audit log the tenant keeps, as far as we know (7 without Entra ID P1, else 30)."""
    window = window or {}
    if window.get("retention_days") == FREE_RETENTION_DAYS:
        return FREE_RETENTION_DAYS
    return window.get("requested_days") or WINDOW_DAYS


def window_text(window: dict | None) -> str:
    """How far back the stored events go, in words ("the last 30 days")."""
    window = window or {}
    retention, oldest = window.get("retention_days"), parse_ts(window.get("oldest"))
    if retention == FREE_RETENTION_DAYS:
        text = (f"the last {FREE_RETENTION_DAYS} days (the tenant's audit log keeps only "
                f"{FREE_RETENTION_DAYS} days without Entra ID P1)")
    elif retention or not oldest:
        text = f"the last {window.get('requested_days') or WINDOW_DAYS} days"
    else:
        text = f"the period since {oldest:%d %b %Y}"
    if window.get("capped"):
        text += (". Part of the audit log hasn't been read yet (the next collection carries on from there), "
                 "so some changes may be missing")
    return text


HISTORY_DAYS = 365  # how far back the Admin activity report and account pages read the activity history


def from_history(conn, tenant_id: int, start, end) -> tuple[list[dict], object] | None:
    """The changes worth knowing about between ``start`` and ``end`` from the activity history, newest first,
    and the history's coverage; None when the tenant's audit log has never been read into it."""
    from . import events

    cov = events.coverage(conn, tenant_id, "audits")
    if cov.detailed_from is None:
        return None
    keep = lambda category, activity: kind_of({"category": category, "activityDisplayName": activity})  # noqa: E731
    found = [normalise(e) for e in events.audits_between(conn, tenant_id, start, end, keep=keep)]
    return found, cov
