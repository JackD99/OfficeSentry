"""The page for one account, put together from the parts."""

from __future__ import annotations

from datetime import UTC, datetime

from ... import devices
from ...checks import RULES
from ...licences import is_stale, last_sign_in
from ...snapshot import TenantSnapshot
from ..data_reports import Highlight
from ..identity_reports import P1
from .base import INACTIVE_DAYS, PARTS, SEVERITY_RANK, Person, Profile, _day, _when
from .history import history
from .licences import _licences


def find_user(snap: TenantSnapshot, user_id: str) -> dict | None:
    return next((u for u in snap.objects("users", "user") or [] if u["_id"] == user_id), None)


def _findings(snap: TenantSnapshot, person: Person) -> list[dict]:
    titles = {r.key: r.title for r in RULES}
    rows = snap.conn.execute(
        "SELECT * FROM findings WHERE tenant_id = ? AND resolved_at IS NULL", (snap.tenant["id"],)
    ).fetchall()
    out = [
        dict(r, title=titles.get(r["rule_key"], r["rule_key"]))
        for r in rows
        if r["object_id"] == person.id or person.named(r["subject"])
    ]
    out.sort(key=lambda f: (SEVERITY_RANK.get(f["severity"], 4), f["title"]))
    return out


def _account_facts(p: Person) -> list[tuple[str, object]]:
    u = p.user
    facts: list[tuple[str, object]] = [("Name", u.get("displayName") or ""), ("Sign-in name", p.upn)]
    if u.get("mail") and u["mail"].lower() != p.upn.lower():
        facts.append(("Email", u["mail"]))
    kind = "Guest" if p.guest else "Member"
    if p.guest and u.get("externalUserState"):
        kind += {"PendingAcceptance": ", invitation not accepted", "Accepted": ", invitation accepted"}.get(
            u["externalUserState"], ""
        )
    facts.append(("Account type", kind))
    facts.append(("Sign-in", "Allowed" if p.enabled else "Blocked"))
    facts.append(("Source", "Synced from on-premises AD" if u.get("onPremisesSyncEnabled") else "Cloud-only"))
    if u.get("createdDateTime"):
        facts.append(("Created", _when(u["createdDateTime"], p.now)))
    if u.get("lastPasswordChangeDateTime"):
        facts.append(("Password last changed", _when(u["lastPasswordChangeDateTime"], p.now)))
    facts.append(
        (
            "Password expires",
            "Never (set to never expire)" if u.get("passwordNeverExpires") else "Follows the organisation's policy",
        )
    )
    return facts


def _account_attention(p: Person) -> None:
    u = p.user
    if not p.enabled:
        return
    stale = is_stale(u, p.now, INACTIVE_DAYS)
    last = last_sign_in(u)
    if stale and last:
        p.flag(
            "medium" if u.get("licensed") else "low",
            f"No sign-in for {(p.now - last).days} days. If they've left, block sign-in and reclaim the licences.",
            "activity",
        )
    elif stale:
        p.flag("low", f"Has never signed in (created {_day(u.get('createdDateTime')) or 'a while ago'}).", "activity")
    if p.guest and u.get("externalUserState") == "PendingAcceptance":
        created = devices.parse_ts(u.get("createdDateTime"))
        if created and (p.now - created).days > 30:
            p.flag("low", f"Invited {_day(created)} and never accepted. Remove the invitation if it's not needed.")


def build(conn, tenant, user_id: str, now: datetime | None = None, leaver: bool = False) -> Profile | None:
    """The page for one account, or None when the latest Users snapshot doesn't have it."""
    now = now or datetime.now(UTC)
    snap = TenantSnapshot(conn, tenant)
    user = find_user(snap, user_id)
    if user is None:
        return None
    p = Person(snap, user, now)
    p.leaver = leaver  # show the leaver checklist even though sign-in is allowed
    profile = Profile(dict(tenant), user_id, user, now, user.get("displayName") or p.label, p.upn)
    _account_attention(p)
    for _, builder in PARTS:
        result = builder(p)
        if result is not None:
            p.link(result.tables)
            profile.parts.append(result)
    profile.account = _account_facts(p)
    profile.attention = sorted(p.attention, key=lambda a: SEVERITY_RANK.get(a.severity, 4))
    profile.findings = _findings(snap, p)
    _, skus = _licences(p)
    profile.history, profile.history_since = history(snap, user_id, skus)

    parts = {x.key: x for x in profile.parts}
    profile.badges = [
        ("Guest" if p.guest else "Member", None),
        ("Sign-in allowed", "ok") if p.enabled else ("Sign-in blocked", "bad"),
        ("Synced from AD" if user.get("onPremisesSyncEnabled") else "Cloud-only", None),
    ]
    if p.admin:
        profile.badges.append(("Admin", "warn"))
    last = last_sign_in(user)
    sign_in_foot = (
        (f"Last sign-in {_when(last, now).split(' (')[-1].rstrip(')')}" if last else "Never signed in")
        if user.get("signInActivityAvailable")
        else f"Sign-in dates need {P1}"
    )
    stale = p.enabled and is_stale(user, now, INACTIVE_DAYS)
    worst = profile.attention[0].severity if profile.attention else None
    profile.tiles = [
        Highlight(
            "Needs attention",
            len(profile.attention),
            "Nothing to do" if not profile.attention else f"Worst: {worst}",
            "bad" if worst in ("critical", "high") else ("warn" if worst else "ok"),
        ),
        Highlight("Sign-in", "Allowed" if p.enabled else "Blocked", sign_in_foot, "warn" if stale else None),
    ]
    for key, label in (("mfa", "MFA"), ("roles", "Admin roles"), ("licences", "Licences"), ("devices", "Devices")):
        x = parts.get(key)
        if x is None:
            continue
        value = "Not checked" if x.unavailable else (x.summary or "None")
        foot = ""
        if key == "licences" and "," in value:
            value, foot = value.split(", ", 1)
        elif key == "devices" and "," in value:
            value, foot = value.split(", ", 1)
        elif key == "roles" and not x.unavailable and x.summary != "None":
            n = len(x.tables[0].rows) if x.tables else 0
            value, foot = str(n), x.summary
        profile.tiles.append(Highlight(label, value, foot, x.tone))
    sources = (
        "users",
        "mfa_registration",
        "conditional_access",
        "admin_roles",
        "groups",
        "mailboxes",
        "inbox_rules",
        "mailbox_usage",
        "devices",
        "sharepoint_usage",
        "shared_files",
        "app_consents",
        "legacy_signins",
        "sign_ins",
        "admin_activity",
    )
    from ...collectors import REGISTRY

    profile.sources = [
        (REGISTRY[c].title, (snap.runs.get(c) or {"finished_at": None})["finished_at"])
        for c in sources
        if c in REGISTRY
    ]
    return profile
