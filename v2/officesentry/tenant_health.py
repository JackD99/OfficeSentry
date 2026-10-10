"""Is a client tenant set up and collecting properly? Plain-English answers with fixes.

Built from what is already stored (the tenant row, the latest runs and the
``connection`` collector's snapshot), never from Graph, so pages that show it
load fast. ``assess()`` returns:

* ``checks``: one line per thing that can go wrong (app connection, consent,
  sign-in, permissions, Global Reader, licences, collection, data sources),
  each ``ok``, ``bad``, ``warn``, ``todo`` (an onboarding step not done yet),
  ``pending`` (being checked) or ``unknown`` (not checked yet), with the fix.
* ``steps``: the onboarding checklist, ticked off from the same state.
* ``status``: ``onboarding``, ``healthy``, ``attention``, ``paused`` or ``archived``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import store
from .collectors import REGISTRY
from .entra import EXCHANGE_PERMISSIONS, GRAPH_PERMISSIONS
from .formatting import capability
from .schedule import nightly_collectors
from .text import plural

ISO = "%Y-%m-%dT%H:%M:%SZ"
ALL_PERMISSIONS = len(GRAPH_PERMISSIONS) + len(EXCHANGE_PERMISSIONS)
SIGN_IN_PREFIX = "Sign-in failed: "
STATUS = {  # status -> (label, pill tone)
    "onboarding": ("Onboarding", "accent"),
    "healthy": ("Healthy", "ok"),
    "attention": ("Needs attention", "bad"),
    "paused": ("Paused", "info"),
    "archived": ("Archived", "info"),
}
GLOBAL_READER_STEPS = (
    "Microsoft Entra admin centre → Roles and admins → Global Reader → Add assignments, "
    "then select the Office Sentry app"
)


@dataclass
class Check:
    key: str
    title: str
    state: str  # ok | bad | warn | todo | pending | unknown | info
    summary: str
    fix: str = ""
    action: str = ""  # what the page offers next to it: consent | check | collect | collection | connection

    @property
    def problem(self) -> bool:
        return self.state in ("bad", "warn")


@dataclass
class Step:
    key: str
    title: str
    done: bool
    todo: str = ""  # the step as an instruction, for "Next: ..."


@dataclass
class Health:
    tenant: object
    status: str
    checks: list[Check]
    steps: list[Step]
    result: dict | None  # the connection collector's latest snapshot
    latest: dict  # collector -> latest run (any status)
    last_success: str | None
    full_collection: bool
    collecting: bool
    failing: list[tuple[str, str, str]] = field(default_factory=list)  # (key, title, error)

    @property
    def label(self) -> str:
        return STATUS[self.status][0]

    @property
    def tone(self) -> str:
        return STATUS[self.status][1]

    @property
    def onboarded(self) -> bool:
        return all(s.done for s in self.steps)

    @property
    def problems(self) -> list[Check]:
        return [c for c in self.checks if c.problem]

    @property
    def next_step(self) -> Step | None:
        return next((s for s in self.steps if not s.done), None)

    def check(self, key: str) -> Check | None:
        return next((c for c in self.checks if c.key == key), None)


def latest_runs(conn, tenant_id: int) -> dict:
    rows = conn.execute(
        "SELECT r.* FROM runs r JOIN (SELECT collector, MAX(id) AS id FROM runs WHERE tenant_id = ?"
        " GROUP BY collector) latest ON latest.id = r.id",
        (tenant_id,),
    ).fetchall()
    return {r["collector"]: r for r in rows}


def connection_result(conn, tenant_id: int) -> dict | None:
    run = store.latest_run(conn, tenant_id, "connection")
    if run is None:
        return None
    rows = store.run_objects(conn, run["id"], "connection")
    return dict(rows[0], checked_at=run["finished_at"]) if rows else None


def _succeeded(conn, tenant_id: int) -> dict[str, str]:
    """collector -> when it last succeeded."""
    rows = conn.execute(
        "SELECT collector, MAX(finished_at) AS at FROM runs WHERE tenant_id = ?"
        " AND status IN ('succeeded', 'partial') GROUP BY collector",
        (tenant_id,),
    ).fetchall()
    return {r["collector"]: r["at"] for r in rows}


def _title(key: str) -> str:
    return REGISTRY[key].title if key in REGISTRY else key


def _explain(error: str) -> str:
    return (error or "no details").removeprefix(SIGN_IN_PREFIX).splitlines()[0][:400]


def assess(conn, tenant, now: datetime | None = None) -> Health:
    from .reports.monthly_review import STALE_AFTER

    now = now or datetime.now(UTC)
    connection = store.get_connection(conn, tenant["connection_id"]) if tenant["connection_id"] else None
    latest = latest_runs(conn, tenant["id"])
    succeeded = _succeeded(conn, tenant["id"])
    result = connection_result(conn, tenant["id"])
    nightly = nightly_collectors()
    full = all(k in succeeded for k in nightly)
    collecting = any(r["status"] in ("queued", "running") for r in latest.values())
    last_success = max(succeeded.values(), default=None)
    name = tenant["name"]
    checks: list[Check] = []

    # -- App connection and consent -----------------------------------------------------
    if not connection:
        checks.append(
            Check(
                "connection",
                "App connection",
                "bad",
                "No app connection chosen.",
                "Choose the app connection under Settings on this page.",
                "connection",
            )
        )
    elif not connection["client_id"]:
        checks.append(
            Check(
                "connection",
                "App connection",
                "bad",
                f"{connection['name']} has no application (client) ID yet.",
                "Add it under Settings → App connection.",
            )
        )
    else:
        checks.append(Check("connection", "App connection", "ok", connection["name"]))

    if tenant["consented_at"]:
        checks.append(Check("consent", "Admin consent", "ok", "Granted.", action="reconsent"))
    else:
        checks.append(
            Check(
                "consent",
                "Admin consent",
                "todo",
                "Not granted yet.",
                f"A Global Administrator of {name} opens the consent link and approves the read-only "
                "permissions. If they already have, press Check connection now.",
                "consent",
            )
        )

    # -- Sign-in -------------------------------------------------------------------------
    conn_run = latest.get("connection")
    if conn_run is not None and conn_run["status"] in ("queued", "running"):
        checks.append(Check("sign_in", "Sign-in", "pending", "Checking now…"))
    elif conn_run is not None and conn_run["status"] == "failed":
        error = conn_run["error"] or ""
        if error.startswith(SIGN_IN_PREFIX):
            title, _, fix = _explain(error).partition(". ")
            checks.append(
                Check(
                    "sign_in",
                    "Sign-in",
                    "bad",
                    f"Office Sentry can't sign in. {title}.",
                    fix or "See the run log under Data collection.",
                    "check",
                )
            )
        else:
            checks.append(
                Check("sign_in", "Sign-in", "bad", "The last connection check failed.", _explain(error), "check")
            )
    elif last_success:
        checks.append(Check("sign_in", "Sign-in", "ok", "Office Sentry signs in and reads the tenant."))
    elif any(r["status"] == "failed" for r in latest.values()):
        failed = next(r for r in latest.values() if r["status"] == "failed")
        checks.append(
            Check("sign_in", "Sign-in", "bad", "Collection has failed so far.", _explain(failed["error"]), "check")
        )
    else:
        checks.append(
            Check(
                "sign_in",
                "Sign-in",
                "unknown",
                "Not checked yet.",
                "Press Check connection now once consent is granted.",
                "check",
            )
        )

    # -- Permissions and Global Reader ----------------------------------------------------
    checks.extend(_app_checks(result, name))

    # -- Licences ---------------------------------------------------------------------------
    caps = store.tenant_capabilities(tenant)
    if "tenant_profile" in succeeded:
        found = [capability(k) for k, v in sorted(caps.items()) if v]
        checks.append(
            Check(
                "licences",
                "Licences found",
                "info",
                ", ".join(found) if found else "None of the features Office Sentry checks for.",
                "" if found else "Checks that need a licence show as not checked.",
            )
        )
    else:
        checks.append(Check("licences", "Licences found", "unknown", "Found by the first collection."))

    # -- Collection ---------------------------------------------------------------------------
    stale_cutoff = (now - STALE_AFTER).strftime(ISO)
    if full:
        oldest = min(succeeded[k] for k in nightly)
        if oldest < stale_cutoff and tenant["enabled"]:
            checks.append(
                Check(
                    "collection",
                    "Collection",
                    "warn",
                    "Some data is more than 36 hours old.",
                    "Check the worker is running. Data sources that keep failing are listed below.",
                    "collect",
                )
            )
        else:
            checks.append(Check("collection", "Collection", "ok", "Every data source has been collected."))
    elif collecting:
        checks.append(
            Check(
                "collection",
                "Collection",
                "pending",
                f"First full collection in progress: {len([k for k in nightly if k in succeeded])} "
                f"of {len(nightly)} data sources done.",
            )
        )
    else:
        done = len([k for k in nightly if k in succeeded])
        checks.append(
            Check(
                "collection",
                "Collection",
                "todo",
                f"First full collection not done yet ({done} of {len(nightly)} data sources).",
                "Collect everything now, or wait for the nightly collection.",
                "collect",
            )
        )

    failing = [(k, _title(k), r["error"] or "") for k, r in latest.items() if r["status"] == "failed" and k in REGISTRY]
    if failing:
        first = failing[0][2]
        fix = _explain(first)
        if "Global Reader" in first or "Exchange refused access" in first:
            fix = f"Assign Global Reader: {GLOBAL_READER_STEPS}."
        checks.append(
            Check(
                "sources",
                "Data sources",
                "bad",
                f"{plural(len(failing), 'data source')} failing: " + ", ".join(t for _, t, _ in failing) + ".",
                fix,
                "collection",
            )
        )
    elif latest:
        checks.append(Check("sources", "Data sources", "ok", "None failing."))

    steps = [
        Step("added", "Tenant added", True),
        Step("consent", "Admin consent granted", bool(tenant["consented_at"]), "grant admin consent"),
        Step(
            "global_reader",
            "Global Reader assigned",
            bool(result and result.get("global_reader")),
            "assign Global Reader to the app",
        ),
        Step(
            "collection",
            "First full collection done",
            full,
            "first full collection (collecting now)" if collecting else "collect everything",
        ),
        Step(
            "permissions",
            "All permissions granted",
            bool(result and result.get("permissions_checked") and not result.get("missing")),
            "re-grant admin consent for the missing permissions"
            if result and result.get("missing")
            else "check the permissions",
        ),
    ]

    onboarding_steps = {"consent", "permissions", "global_reader", "collection"}
    if tenant["archived_at"]:
        status = "archived"
    elif not tenant["enabled"]:
        status = "paused"
    elif not tenant["consented_at"] or not full:
        # While onboarding, missing steps are the checklist, not problems; a sign-in failure still is.
        status = "attention" if any(c.problem and c.key not in onboarding_steps for c in checks) else "onboarding"
    elif any(c.problem for c in checks):
        status = "attention"
    else:
        status = "healthy"
    return Health(
        tenant=tenant,
        status=status,
        checks=checks,
        steps=steps,
        result=result,
        latest=latest,
        last_success=last_success,
        full_collection=full,
        collecting=collecting,
        failing=failing,
    )


def _app_checks(result: dict | None, name: str) -> list[Check]:
    if result is None:
        return [
            Check(
                "permissions",
                "Permissions",
                "unknown",
                "Not checked yet.",
                "Checked with every collection, or press Check connection now.",
                "check",
            ),
            Check(
                "global_reader",
                "Global Reader role",
                "unknown",
                "Not checked yet.",
                "Checked with every collection, or press Check connection now.",
                "check",
            ),
        ]
    if not result.get("client_id"):
        return [Check("permissions", "Permissions", "unknown", "Not checked: the tenant has no app connection.")]
    if result.get("app_found") is False:
        fix = (
            f"A Global Administrator of {name} grants admin consent with the consent link. If they deleted the "
            "app on purpose, offboard this tenant."
        )
        return [Check("permissions", "Permissions", "bad", "Office Sentry's app isn't in this tenant.", fix, "consent")]
    checks = []
    app = result.get("app_name") or "the Office Sentry app"
    if result.get("app_enabled") is False:
        checks.append(
            Check(
                "app_enabled",
                "App enabled",
                "bad",
                "The app is disabled in this tenant.",
                f"In {name}: Microsoft Entra admin centre → Enterprise applications → {app} → "
                "Properties → set Enabled for users to sign in to Yes.",
            )
        )
    missing = result.get("missing") or []
    if not result.get("permissions_checked"):
        checks.append(
            Check(
                "permissions",
                "Permissions",
                "warn",
                "Couldn't read which permissions are granted.",
                f"A Global Administrator of {name} opens the consent link again; it grants every permission at once.",
                "consent",
            )
        )
    elif missing:
        checks.append(
            Check(
                "permissions",
                "Permissions",
                "bad",
                f"{len(missing)} of {ALL_PERMISSIONS} not granted: {', '.join(missing)}.",
                f"A Global Administrator of {name} opens the consent link again; it asks for every "
                "permission at once. Then press Check connection now.",
                "consent",
            )
        )
    else:
        checks.append(Check("permissions", "Permissions", "ok", f"All {ALL_PERMISSIONS} granted."))
    extra = extra_permissions(result)
    if extra:
        checks.append(
            Check(
                "extra_permissions",
                "Extra permissions",
                "warn",
                f"The app has {plural(len(extra), 'permission')} Office Sentry doesn't ask for: {', '.join(extra)}.",
                "Office Sentry only needs the read-only permissions listed on its App connection page, so a stolen "
                "key shouldn't be able to do more. Remove the others from the app registration's API permissions in "
                f"your own tenant, then in {name}: Microsoft Entra admin centre → Enterprise applications → {app} → "
                "Permissions, revoke them. Then press Check connection now.",
                "check",
            )
        )

    if not result.get("roles_checked"):
        checks.append(
            Check(
                "global_reader",
                "Global Reader role",
                "warn",
                "Couldn't read the app's admin roles.",
                "Re-grant admin consent (RoleManagement.Read.Directory lets Office Sentry check this).",
                "consent",
            )
        )
    elif result.get("global_reader"):
        extra = extra_roles(result)
        if extra:
            roles = " and ".join(extra)
            checks.append(
                Check(
                    "global_reader",
                    "Global Reader role",
                    "warn",
                    f"Assigned, but the app also holds {roles}.",
                    f"Global Reader is all Office Sentry needs, and it can't change anything. In {name}: "
                    f"Microsoft Entra admin centre → Roles and admins → {extra[0]} → select {app} → "
                    "Remove assignments"
                    + (", and the same for the others." if len(extra) > 1 else ".")
                    + " Then press Check connection now.",
                    "check",
                )
            )
        else:
            checks.append(Check("global_reader", "Global Reader role", "ok", "Assigned."))
    else:
        others = [r for r in result.get("roles") or [] if r != "Global Reader"]
        summary = "Not assigned, so mailbox reports can't be collected."
        if others:
            summary = (
                f"Not assigned (the app holds {', '.join(others)} instead), so mailbox reports may not be collected."
            )
        checks.append(
            Check(
                "global_reader",
                "Global Reader role",
                "bad",
                summary,
                f"In {name}: {GLOBAL_READER_STEPS.removesuffix('the Office Sentry app')}{app}. "
                "Microsoft can take up to an hour to apply it; then press Check connection now.",
                "check",
            )
        )
    return checks


# Granted alongside what Office Sentry asks for but no more access: Exchange's newer name for
# Exchange.ManageAsApp, which is still limited to what the app's admin role (Global Reader) allows.
NOT_EXTRA = {"Exchange.ManageAsAppV2"}


def extra_permissions(result: dict | None) -> list[str]:
    """Permissions the tenant granted the app beyond what Office Sentry asks for."""
    r = result or {}
    return sorted(set(r.get("other") or []) - NOT_EXTRA) if r.get("permissions_checked") else []


def extra_roles(result: dict | None) -> list[str]:
    """Admin roles the app holds besides Global Reader (the only one it needs, and read-only)."""
    r = result or {}
    return [role for role in r.get("roles") or [] if role != "Global Reader"] if r.get("roles_checked") else []


def counts(health: Health) -> dict:
    """Short cell texts for the all-tenants admin table."""
    r = health.result or {}
    missing = r.get("missing") or []
    return {
        "consent": bool(health.tenant["consented_at"]),
        "missing": missing if r.get("permissions_checked") else None,
        "extra_permissions": extra_permissions(r),
        "global_reader": r.get("global_reader") if r.get("roles_checked") else None,
        "extra_roles": extra_roles(r),
        "failing": health.failing,
    }


def describe(health: Health) -> str:
    """One sentence about what to do next, for lists."""
    if health.status == "archived":
        return "Archived: hidden from lists and reports. Restore it to bring it back."
    if health.status == "paused":
        return "Collection and emailed reports are paused."
    step = health.next_step
    problems = health.problems
    if problems and health.status != "onboarding":
        first = problems[0]
        more = len(problems) - 1
        summary = first.summary[0].lower() + first.summary[1:]
        return f"{first.title}: {summary}" + (f" (and {plural(more, 'other problem')})" if more else "")
    if step:
        return f"Next: {step.todo or step.title[0].lower() + step.title[1:]}."
    return "Everything is working."
