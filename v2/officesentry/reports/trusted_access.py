"""Trusted organisations and locations: what other organisations' people can do here, whose sign-in checks
the tenant accepts, and which networks and countries Conditional Access treats specially.

Reads the cross-tenant access settings (``collectors/policies.py``: the default for every other organisation
and each organisation given its own) and the named locations (``collectors/identity.py``), with the
Conditional Access policies that use each location.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime

from ..snapshot import TenantSnapshot
from ..text import plural
from .data_reports import DataReport, Rows, Section, Tile
from .registry import report

NOT_COLLECTED = (
    "Cross-tenant access settings and named locations haven't been collected yet. Refresh \"Sharing and consent "
    'settings" and "Conditional Access" on the client page.'
)
DEFAULTS = "Default for other organisations"
PARTNERS = "Organisations with their own settings"
LOCATIONS = "Named locations"
NOT_USED = "Not used"
# An IP range this wide (fewer prefix bits) or wider, trusted or left out of a policy, is flagged.
WIDE = {4: 16, 6: 32}


def _who(part: dict, everyone: str, all_text: str, one: str, many: str) -> str:
    targets = part.get("targets") or []
    if everyone in targets:
        return all_text
    if targets == ["Office365"]:
        return "Office 365"
    return plural(part.get("count") or len(targets), one, many)


def describe(side: dict | None) -> str:
    """One B2B setting in words: "Allowed for all users, all apps", "Blocked", or "Default" for an organisation
    that leaves it to the default."""
    if side is None:
        return "Default"
    users, apps = side.get("usersAndGroups"), side.get("applications")
    parts = []
    if users:
        parts.append(
            (users.get("access") or "", _who(users, "AllUsers", "all users", "user or group", "users or groups"))
        )
    if apps:
        parts.append((apps.get("access") or "", _who(apps, "AllApplications", "all apps", "app", "apps")))
    if not parts:
        return "Default"
    if all(access == "blocked" for access, _ in parts) and all(w.startswith("all ") for _, w in parts):
        return "Blocked"
    if len({access for access, _ in parts}) == 1:
        return f"{parts[0][0].capitalize()} for " + ", ".join(w for _, w in parts)
    return "; ".join(f"{label}: {access} for {w}" for label, (access, w) in zip(("Users", "Apps"), parts, strict=False))


def _allows(side: dict | None) -> bool:
    return bool(side) and any((part or {}).get("access") == "allowed" for part in side.values())


def _yes(value) -> str:
    return "Yes" if value else "No"


def _trust_text(trust: dict | None) -> tuple[str, str]:
    """(MFA, devices) for an organisation's trust settings; "Default" when it leaves them to the default."""
    if trust is None:
        return "Default", "Default"
    devices = [
        label
        for key, label in (
            ("isCompliantDeviceAccepted", "compliant"),
            ("isHybridAzureADJoinedDeviceAccepted", "hybrid joined"),
        )
        if trust.get(key)
    ]
    return _yes(trust.get("isMfaAccepted")), (", ".join(devices).capitalize() if devices else "No")


def _wide(cidr: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    """The range, when it's wide enough to flag (and not a private range, which no cloud sign-in comes from)."""
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return None
    if net.is_private or net.prefixlen > WIDE[net.version]:
        return None
    return net


def _uses(policies: list[dict], loc_id: str, trusted: bool) -> tuple[list[str], list[str]]:
    """The policies that include and those that leave out a location (directly, or as one of the trusted
    locations), each with "(off)" or "(report-only)" when it isn't enforced."""
    included, excluded = [], []
    for p in sorted(policies, key=lambda p: (p.get("displayName") or "").lower()):
        cond = (p.get("conditions") or {}).get("locations") or {}
        name = p.get("displayName") or "Unnamed policy"
        state = {"disabled": " (off)", "enabledForReportingButNotEnforced": " (report-only)"}.get(p.get("state"), "")
        for ids, into in ((cond.get("includeLocations"), included), (cond.get("excludeLocations"), excluded)):
            ids = ids or []
            if loc_id in ids or (trusted and "AllTrusted" in ids):
                into.append(name + state)
    return included, excluded


@report(
    "trusted-access",
    "Trusted organisations and locations",
    "What other organisations' people can do here and whose MFA the tenant accepts (cross-tenant access), and "
    "the named locations Conditional Access trusts or blocks, with the policies using each.",
    ("tenant_policies", "conditional_access"),
    group="identity",
    order=45,
    keywords="cross-tenant access b2b collaboration direct connect teams shared channels external "
    "organisations partners inbound outbound trust mfa named locations trusted ips ip ranges countries "
    "geo blocking conditional access",
)
def trusted_access_report(snap: TenantSnapshot, now: datetime, out: DataReport) -> None:
    defaults = snap.objects("tenant_policies", "cross_tenant_default")
    partners = snap.objects("tenant_policies", "cross_tenant_partner")
    statuses = snap.objects("conditional_access", "named_location_status") or []
    location_state = statuses[0].get("state") if statuses and isinstance(statuses[0], dict) else None
    locations = snap.objects("conditional_access", "named_location") if location_state == "read" else None
    default = defaults[0] if defaults and isinstance(defaults[0], dict) else None
    if default is None and location_state is None:
        out.unavailable = NOT_COLLECTED
        return

    settings = Section(
        DEFAULTS,
        ["Setting", "Their people here (inbound)", "Your people there (outbound)"],
        intro="What applies to every organisation without settings of its own. B2B collaboration is guest "
        "accounts; B2B direct connect is Teams shared channels without a guest account. Trusting another "
        "organisation's MFA or devices lets its people pass your Conditional Access with checks made at home.",
        empty="The cross-tenant access settings haven't been read yet." if default is None else "No settings.",
    )
    if default is not None:
        trust = default.get("trust") or {}
        settings.add(
            [
                "B2B collaboration",
                describe(default.get("b2bCollaborationInbound")),
                describe(default.get("b2bCollaborationOutbound")),
            ]
        )
        settings.add(
            [
                "B2B direct connect",
                describe(default.get("b2bDirectConnectInbound")),
                describe(default.get("b2bDirectConnectOutbound")),
            ],
            "low" if _allows(default.get("b2bDirectConnectInbound")) else None,
        )
        settings.add(
            ["Trust their MFA", _yes(trust.get("isMfaAccepted")), ""], "medium" if trust.get("isMfaAccepted") else None
        )
        settings.add(
            ["Trust their compliant devices", _yes(trust.get("isCompliantDeviceAccepted")), ""],
            "low" if trust.get("isCompliantDeviceAccepted") else None,
        )
        settings.add(
            ["Trust their hybrid joined devices", _yes(trust.get("isHybridAzureADJoinedDeviceAccepted")), ""],
            "low" if trust.get("isHybridAzureADJoinedDeviceAccepted") else None,
        )

    orgs = Section(
        PARTNERS,
        [
            "Tenant ID",
            "Relationship",
            "B2B collaboration in",
            "B2B collaboration out",
            "Direct connect in",
            "Direct connect out",
            "Trusts their MFA",
            "Trusts their devices",
            "Invitations redeemed automatically",
        ],
        intro='Organisations given settings of their own, which replace the default for them. "Default" means '
        "that setting follows the default above. Microsoft gives each organisation's tenant ID, not its name: "
        "the Entra admin centre names them under External Identities > Cross-tenant access settings.",
        empty="No organisation has settings of its own." if default is not None else "Not read yet.",
        optional=("Invitations redeemed automatically",),
    )
    for p in sorted((p for p in partners or [] if isinstance(p, dict)), key=lambda p: p.get("_id") or ""):
        mfa, devices = _trust_text(p.get("trust"))
        relationship = ", ".join(
            label
            for key, label in (("serviceProvider", "Service provider"), ("multitenant", "Multitenant organisation"))
            if p.get(key)
        )
        redeem = [d for key, d in (("consentInbound", "inbound"), ("consentOutbound", "outbound")) if p.get(key)]
        orgs.add(
            [
                p.get("_id"),
                relationship,
                describe(p.get("b2bCollaborationInbound")),
                describe(p.get("b2bCollaborationOutbound")),
                describe(p.get("b2bDirectConnectInbound")),
                describe(p.get("b2bDirectConnectOutbound")),
                mfa,
                devices,
                ", ".join(redeem).capitalize() if redeem else "No",
            ]
        )

    policies = [p for p in snap.objects("conditional_access", "ca_policy") or [] if isinstance(p, dict)]
    places = Section(
        LOCATIONS,
        ["Location", "Kind", "Trusted", "Ranges or countries", "Used by", "Last changed"],
        intro="Networks and countries Conditional Access policies can include or leave out. A trusted location "
        'counts as one of "all trusted locations", which policies often leave out of MFA, so a wide range '
        f"marked trusted (wider than /{WIDE[4]} for IPv4 or /{WIDE[6]} for IPv6) or left out of a policy is "
        "flagged.",
        empty={
            "read": "No named locations.",
            "unavailable": "No named locations: they're part of Conditional Access, which needs Entra ID P1.",
            "failed": "Named locations couldn't be read; see the Conditional Access collection's log.",
        }.get(
            location_state or "",
            'Named locations haven\'t been read yet. Refresh "Conditional Access" on the client page.',
        ),
    )
    for loc in sorted(
        (loc for loc in locations or [] if isinstance(loc, dict)), key=lambda loc: (loc.get("name") or "").lower()
    ):
        trusted = loc.get("trusted") is True
        included, excluded = _uses(policies, loc.get("_id") or "", trusted)
        used = "; ".join(
            part
            for part in (
                f"Included in {', '.join(included)}" if included else "",
                f"Left out of {', '.join(excluded)}" if excluded else "",
            )
            if part
        )
        if loc.get("kind") == "countries":
            what = ", ".join(loc.get("countries") or []) + (" and unknown places" if loc.get("unknown") else "")
            kind, flag = "Countries", None
        else:
            ranges = loc.get("ranges") or []
            what = ", ".join(ranges)
            kind = "IP ranges"
            wide = [net for net in map(_wide, ranges) if net is not None] if trusted or excluded else []
            flag = ("high" if any(net.prefixlen == 0 for net in wide) else "medium") if wide else None
        places.add(
            [
                loc.get("name"),
                kind,
                _yes(trusted),
                what,
                used or NOT_USED,
                (loc.get("modified") or loc.get("created") or "")[:10],
            ],
            flag,
        )
    out.sections = [settings, orgs, places]

    if default is not None:
        out.tiles.append(
            Tile(
                "Organisations with own settings",
                len(orgs.rows),
                "Each replaces the default for that organisation",
                rows=Rows(PARTNERS),
            )
        )
        outside_mfa = bool((default.get("trust") or {}).get("isMfaAccepted"))
        out.tiles.append(
            Tile(
                "Outside MFA trusted",
                "Yes" if outside_mfa else "No",
                "By default, from every organisation",
                "warn" if outside_mfa else "ok",
                Rows(DEFAULTS, {"Setting": "Trust their MFA"}),
            )
        )
    if locations is not None:
        out.tiles += [
            Tile(
                "Trusted locations",
                sum(r[2] == "Yes" for r in places.rows),
                "IP ranges marked trusted",
                rows=Rows(LOCATIONS, {"Trusted": "Yes"}),
            ),
            Tile(
                "Locations not used",
                sum(r[4] == NOT_USED for r in places.rows),
                "No policy includes or leaves them out",
                rows=Rows(LOCATIONS, {"Used by": NOT_USED}),
            ),
        ]
    out.footnote = (
        "From the tenant's cross-tenant access settings and Conditional Access named locations. Trusting another "
        "organisation's MFA is safest for named partners only, not as the default."
    )
