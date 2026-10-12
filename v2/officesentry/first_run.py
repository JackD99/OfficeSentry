"""The Get started checklist an admin sees until the first collection has run.

Four steps from a fresh install to the first useful report: the app connection, the first client, its
consent and its first collection. Each is read from stored state only (never Graph), so the list ticks
itself off as Office Sentry sees each step done, and ``pending`` returns nothing once something has been
collected, which is when the home pages have real figures to show instead.

The card is drawn by ``templates/_get_started.html``; ``web/home.get_started`` turns each step's ``target``
into a link.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Step:
    key: str
    title: str
    done: bool
    text: str  # what to do, or, once done, what happened
    action: str = ""  # the button on the current step
    target: str = ""  # where it goes: "connections", "new_tenant", "tenant:<id>" or "collection:<id>"
    href: str = ""  # filled in by the web layer


def steps(conn) -> list[Step]:
    """Every step, in order, with whether it's done."""
    connected = conn.execute(
        "SELECT 1 FROM app_connections WHERE client_id IS NOT NULL AND kind = 'main' LIMIT 1"
    ).fetchone()
    first = conn.execute("SELECT id, name FROM tenants WHERE archived_at IS NULL ORDER BY id LIMIT 1").fetchone()
    consented = conn.execute(
        "SELECT id, name FROM tenants WHERE consented_at IS NOT NULL AND archived_at IS NULL"
        " ORDER BY consented_at, id LIMIT 1"
    ).fetchone()
    collected = conn.execute(
        "SELECT 1 FROM runs INDEXED BY idx_runs_status WHERE status IN ('succeeded', 'partial') LIMIT 1"
    ).fetchone()
    tenant = consented or first
    return [
        Step(
            "connection",
            "Connect Office Sentry to Microsoft 365",
            bool(connected),
            "The read-only app is ready."
            if connected
            else "Sign in to Microsoft once, as an admin of your own (the MSP's) tenant. Office Sentry creates its"
            " read-only app with a certificate and keeps nothing from the sign-in.",
            "Set up the app connection",
            "connections",
        ),
        Step(
            "tenant",
            "Add your first client",
            first is not None,
            f"{first['name']} is added."
            if first
            else "Any client you look after. To see your first reports quickly, start with your own tenant.",
            "Add a client",
            "new_tenant",
        ),
        Step(
            "consent",
            "The client grants consent",
            consented is not None,
            f"{consented['name']} approved read-only access."
            if consented
            else "Open the client's page and send its Global Administrator the consent link. They approve"
            " read-only access once; for mailbox reports they also give the app the Global Reader role.",
            f"Open {first['name']}" if first else "Open the client",
            f"tenant:{first['id']}" if first else "",
        ),
        Step(
            "collection",
            "The first collection runs",
            bool(collected),
            "Done. The client's reports, findings and month-end review are filled in."
            if collected
            else "It starts by itself a minute or two after consent and takes a few minutes. Then the client's"
            " reports, findings, licence figures and month-end review fill in, and this list goes away.",
            "See how it's going",
            f"collection:{tenant['id']}" if tenant else "",
        ),
    ]


def pending(conn) -> list[Step] | None:
    """The steps while something is still to do, or None once the first collection has run."""
    found = steps(conn)
    return None if found[-1].done else found


def current(found: list[Step]) -> Step | None:
    """The first step not done."""
    return next((s for s in found if not s.done), None)
