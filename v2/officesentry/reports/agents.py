"""AI agents: the agents with their own identity in a client's Entra ID, what each can reach, and the ones to ask
about first.

Reads what ``collectors/agents.py`` stores, and joins each agent's admin roles (Admin roles) and delegated consents
(Apps and consents), so nothing here calls Microsoft. An agent works on its own, so one that can read mail or files,
change the directory or hold an admin role is worth a word with whoever made it, as is one Microsoft has switched
off.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..checks.apps import HIGH_RISK_PERMISSIONS, SENSITIVE_DELEGATED
from ..devices import parse_ts
from ..permission_batch import waiting_text
from ..snapshot import TenantSnapshot
from ..text import plural
from .data_reports import DataReport, Rows, Section, Tile
from .registry import report

AGENTS = "AI agents"
ACCESS = "What agents can reach"
NEW_DAYS = 30
PERMISSION = "AgentIdentity.Read.All"
NOT_COLLECTED = 'AI agents haven\'t been collected yet. Refresh "AI agents" on the client page.'
FAILED = "Office Sentry couldn't read this client's AI agents; see the AI agents collection's log."
UNAVAILABLE = "Microsoft doesn't offer agent identities (Entra Agent ID) for this tenant."
ON, OFF, MICROSOFT_OFF = "On", "Off", "Switched off by Microsoft"


def _status(snap: TenantSnapshot) -> dict | None:
    rows = snap.objects("agents", "agents_status")
    if rows is None:
        return None
    return rows[0] if rows and isinstance(rows[0], dict) else {}


def _names(snap: TenantSnapshot) -> dict[str, str]:
    """Apps by appId, for which app made an agent and which blueprint it follows."""
    return {
        sp["appId"].lower(): sp["displayName"]
        for sp in snap.objects("app_consents", "service_principal") or []
        if isinstance(sp.get("appId"), str) and isinstance(sp.get("displayName"), str) and sp["displayName"]
    }


def _state(agent: dict) -> str:
    if agent.get("disabledByMicrosoft") not in (None, "NotDisabled"):
        return MICROSOFT_OFF
    return ON if agent.get("enabled") is not False else OFF


def _consented(grants: list[dict]) -> str:
    if any(g.get("consentType") == "AllPrincipals" for g in grants):
        return "Everyone"
    people = {g.get("principalId") for g in grants if g.get("principalId")}
    return plural(len(people), "person", "people") if people else ""


@report(
    "agents",
    "AI agents",
    "The AI agents with their own identity in Entra ID: which app made each, what it can reach on its own or for "
    "people, admin roles it holds, and agents Microsoft has switched off.",
    ("agents", "app_consents", "admin_roles"),
    group="devices",
    order=35,
    keywords="agent id entra agent identity identities copilot studio foundry ai agents bots automation service "
    "principals blueprint permissions agent 365 autonomous",
)
def build(snap: TenantSnapshot, now: datetime, out: DataReport) -> None:
    status = _status(snap)
    if status is None:
        out.unavailable = NOT_COLLECTED
        return
    state = status.get("state")
    if state == "waiting":
        out.unavailable = waiting_text(snap.tenant["name"], [PERMISSION])
        return
    if state == "unavailable":
        out.unavailable = UNAVAILABLE
        return
    if state != "read":
        out.unavailable = FAILED
        return

    names = _names(snap)
    agents = {a["_id"]: a for a in snap.objects("agents", "agent") or [] if isinstance(a.get("_id"), str)}
    held: dict[str, list[dict]] = {}
    for p in snap.objects("agents", "agent_permission") or []:
        if p.get("agentId") in agents and isinstance(p.get("permission"), str):
            held.setdefault(p["agentId"], []).append(p)
    grants: dict[str, list[dict]] = {}
    for g in snap.objects("app_consents", "delegated_grant") or []:
        if g.get("clientId") in agents:
            grants.setdefault(g["clientId"], []).append(g)
    roles: dict[str, list[dict]] = {}
    for r in snap.objects("admin_roles", "role_assignment") or []:
        if r.get("principalId") in agents and isinstance(r.get("roleName"), str):
            roles.setdefault(r["principalId"], []).append(r)

    table = Section(
        AGENTS,
        [
            "Agent",
            "Made with",
            "Blueprint",
            "Created",
            "State",
            "Application permissions",
            "Admin roles",
            "Consented for",
        ],
        intro="Each agent with its own identity in Entra ID. Agents that can read mail or files, change the "
        "directory or hold an admin role on their own are flagged: ask whoever made them whether they need it.",
        empty="No AI agents have their own identity in this tenant.",
        optional=("Blueprint",),
    )
    access = Section(
        ACCESS,
        ["Agent", "API", "Permission", "How", "Given"],
        intro="Every permission an agent holds: on its own (application), or for the people who consented "
        "(delegated). Those that reach mail, files or the whole directory are flagged.",
        empty="No agent holds a permission.",
    )
    rows, reach = [], []
    for aid, a in agents.items():
        name = a.get("displayName") or aid
        app_perms = sorted({p["permission"] for p in held.get(aid, [])})
        risky = sorted(set(app_perms) & HIGH_RISK_PERMISSIONS)
        agent_roles = roles.get(aid, [])
        privileged = any(r.get("privileged") for r in agent_roles)
        delegated = grants.get(aid, [])
        scopes = sorted({s for g in delegated for s in g.get("scopes") or [] if isinstance(s, str)})
        sensitive = sorted(set(scopes) & SENSITIVE_DELEGATED)
        agent_state = _state(a)
        flag = None
        if agent_state == MICROSOFT_OFF or risky or privileged:
            flag = "high"
        elif agent_roles or sensitive:
            flag = "medium"
        created = parse_ts(a.get("created"))
        made = (a.get("createdBy") or "").lower()
        blueprint = (a.get("blueprint") or "").lower()
        rows.append(
            (
                (flag is None, flag != "high", str(name).lower()),
                [
                    name,
                    names.get(made, a.get("createdBy") or ""),
                    names.get(blueprint, a.get("blueprint") or ""),
                    created.date().isoformat() if created else "",
                    agent_state,
                    app_perms,
                    sorted({r["roleName"] for r in agent_roles}),
                    _consented(delegated),
                ],
                flag,
                created,
            )
        )
        for p in held.get(aid, []):
            given = parse_ts(p.get("createdDateTime"))
            reach.append(
                (
                    (str(name).lower(), p.get("resource") or "", p["permission"]),
                    [
                        name,
                        p.get("resource") or "",
                        p["permission"],
                        "On its own",
                        given.date().isoformat() if given else "",
                    ],
                    "high" if p["permission"] in HIGH_RISK_PERMISSIONS else None,
                )
            )
        for g in delegated:
            who = "For everyone" if g.get("consentType") == "AllPrincipals" else "For one person"
            for s in sorted(s for s in g.get("scopes") or [] if isinstance(s, str)):
                reach.append(
                    (
                        (str(name).lower(), g.get("resource") or "", s),
                        [name, g.get("resource") or "", s, who, ""],
                        "medium" if s in SENSITIVE_DELEGATED else None,
                    )
                )
    for _, row, flag, _ in sorted(rows, key=lambda r: r[0]):
        table.add(row, flag)
    for _, row, flag in sorted(reach, key=lambda r: r[0]):
        access.add(row, flag)
    out.sections = [table, access]

    on = sum(r[1][4] == ON for r in rows)
    flagged = sum(r[2] == "high" for r in rows)
    with_roles = sum(bool(r[1][6]) for r in rows)
    since = now - timedelta(days=NEW_DAYS)
    new = sum(1 for r in rows if r[3] and r[3] >= since)
    out.tiles = [
        Tile("AI agents", len(rows), f"{on} switched on", rows=Rows(AGENTS)),
        Tile(
            "Need a look",
            flagged,
            "Reach mail, files or the directory on their own, or hold a powerful role",
            "bad" if flagged else "ok",
            Rows(AGENTS, flagged=True),
        ),
        Tile(
            "With admin roles",
            with_roles,
            "Hold a directory role",
            "warn" if with_roles else "ok",
        ),
        Tile(f"New in {NEW_DAYS} days", new, "Agents created recently"),
    ]
    if status.get("unread"):
        out.notes.append(f"The permissions of {plural(status['unread'], 'agent')} couldn't be read.")
    out.footnote = (
        "From Microsoft Entra Agent ID: agents built with tools such as Copilot Studio or Azure AI Foundry that "
        "were given their own identity. Agents without one act as the person using them and aren't listed. "
        "Change or remove an agent where it was made, or in the Microsoft Entra admin center; Office Sentry "
        "only reads them."
    )
