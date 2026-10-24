"""Adding clients from Partner Center: the MSP's GDAP customers, read once with an admin's own sign-in.

On Onboarding an admin presses "Add from Partner Center". Office Sentry signs them in with the device sign-in the
App connection wizard uses (``app_setup``, purpose "partner"), in the MSP's own tenant, asking only for the
delegated, read-only ``DelegatedAdminRelationship.Read.All`` (``scope``). The one request that receives the token
reads the MSP's GDAP relationships (``relationships``, Microsoft Graph in the MSP's tenant) and lets it go: the
token is never stored, and no refresh token is asked for. What was found (``Customer``, one per customer tenant)
is kept with the sign-in row (``setup_sign_ins.found``) until the admin adds the ones they tick, and is erased
with the row after a day (``app_setup.prune``).

Nothing here reads or writes a client's tenant, and nothing calls Partner Center's own API. Approving Office
Sentry is still each client's consent link; a technician can open it for a client when their GDAP relationship
includes Privileged Role Administrator or Global Administrator (``Customer.can_approve``), the two roles Entra
lets approve application permissions.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

import requests

from . import clouds
from .discovery import tenant_id_for_domain

GUID_RE = re.compile(r"^[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}$")

PRIVILEGED_ROLE_ADMINISTRATOR = "e8611ab8-c189-46e8-94e1-60213ab1f814"
GLOBAL_ADMINISTRATOR = "62e90394-69f5-4237-9190-012177145e10"
# The roles that let a technician approve Office Sentry's application permissions in a customer's tenant, and give
# its app the Global Reader role there.
APPROVING_ROLES = frozenset({PRIVILEGED_ROLE_ADMINISTRATOR, GLOBAL_ADMINISTRATOR})

PAGE_SIZE = 300  # the most Microsoft returns a page
MAX_PAGES = 50
LOOKUPS = 100  # clients added by domain whose tenant ID is looked up, so they aren't offered twice
FIELDS = "id,customer,accessDetails,status,endDateTime"

# A relationship's status in words. Only an active one gives a technician any access.
STATUS_WORDS = {
    "active": "Active",
    "activating": "Being activated",
    "approvalPending": "Waiting for the customer to approve",
    "approved": "Approved, not active yet",
    "created": "Not sent to the customer yet",
    "expired": "Expired",
    "expiring": "Expiring",
    "terminated": "Ended",
    "terminating": "Ending",
    "terminationRequested": "Ending",
}
USABLE = {"active", "expiring"}  # an expiring relationship still works until its end date


class PartnerError(Exception):
    """Why the GDAP relationships couldn't be read, in words for people."""


@dataclass(frozen=True)
class Customer:
    tenant_id: str
    name: str
    status: str  # the best of its relationships' statuses (``STATUS_WORDS``)
    ends: str | None  # when its last usable relationship ends
    can_approve: bool  # a usable relationship includes Privileged Role Administrator or Global Administrator
    relationships: int

    @property
    def usable(self) -> bool:
        return self.status in USABLE

    @property
    def status_words(self) -> str:
        return STATUS_WORDS.get(self.status, self.status or "Unknown")


def scope(cloud: str | None = None) -> str:
    """The one delegated permission the sign-in asks for: reading the MSP's own GDAP relationships."""
    return f"{clouds.get(cloud).graph}/DelegatedAdminRelationship.Read.All"


def _problem(resp: requests.Response) -> str:
    try:
        error = resp.json().get("error")
    except (ValueError, AttributeError):
        error = None
    message = str(error.get("message") or "") if isinstance(error, dict) else ""
    if resp.status_code in (401, 403):
        return (
            "That account can't read your GDAP relationships. Sign in with an account in your own (partner) tenant "
            "that sees them in Partner Center, such as an Admin agent. The first time, a Global Administrator of "
            "your tenant may need to approve Microsoft Graph Command Line Tools reading them."
        )
    return f"Microsoft wouldn't list your GDAP relationships ({resp.status_code}): {message or 'no reason given'}."


def relationships(token: str, cloud: str | None = None, session: requests.Session | None = None) -> list[dict]:
    """Every GDAP relationship of the MSP's tenant, from Microsoft Graph (GET only, paged). Raises
    ``PartnerError`` when Microsoft refuses or can't be reached."""
    http = session or requests.Session()
    graph = clouds.get(cloud).graph
    url: str | None = f"{graph}/v1.0/tenantRelationships/delegatedAdminRelationships"
    params: dict | None = {"$select": FIELDS, "$top": str(PAGE_SIZE)}
    found: list[dict] = []
    for _ in range(MAX_PAGES):
        if url is None:
            break
        try:
            resp = http.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=60)
        except requests.RequestException as exc:
            raise PartnerError("Office Sentry couldn't reach Microsoft Graph to read your GDAP relationships.") from exc
        if not resp.ok:
            raise PartnerError(_problem(resp))
        try:
            body = resp.json()
        except ValueError as exc:
            raise PartnerError("Microsoft's answer about your GDAP relationships couldn't be read.") from exc
        found.extend(r for r in body.get("value") or [] if isinstance(r, dict))
        url, params = body.get("@odata.nextLink"), None
        if url and urlparse(url).netloc != urlparse(graph).netloc:
            url = None  # only ever send the token to Graph itself
    return found


def fold(rows: list[dict]) -> list[Customer]:
    """One ``Customer`` per customer tenant, from its relationships, by name."""
    by_tenant: dict[str, list[dict]] = {}
    for r in rows:
        customer = r.get("customer") if isinstance(r.get("customer"), dict) else {}
        tenant = str(customer.get("tenantId") or "").lower()
        if GUID_RE.match(tenant):
            by_tenant.setdefault(tenant, []).append(r)
    order = list(STATUS_WORDS)
    customers = []
    for tenant, rels in by_tenant.items():
        usable = [r for r in rels if r.get("status") in USABLE]
        best = min(rels, key=lambda r: order.index(r["status"]) if r.get("status") in order else len(order))
        named = next((r for r in usable + rels if (r.get("customer") or {}).get("displayName")), None)
        name = " ".join(str((named or {}).get("customer", {}).get("displayName") or "").split())[:200]
        roles = {
            str(role.get("roleDefinitionId") or "").lower()
            for r in usable
            for role in ((r.get("accessDetails") or {}).get("unifiedRoles") or [])
            if isinstance(role, dict)
        }
        ends = sorted(str(r["endDateTime"]) for r in usable if r.get("endDateTime"))
        customers.append(
            Customer(
                tenant_id=tenant,
                name=name or tenant,
                status=str(best.get("status") or ""),
                ends=ends[-1] if ends else None,
                can_approve=bool(roles & APPROVING_ROLES),
                relationships=len(rels),
            )
        )
    return sorted(customers, key=lambda c: (c.name.lower(), c.tenant_id))


def known_by_domain(conn, cloud: str | None = None, session: requests.Session | None = None) -> dict[str, int]:
    """Clients added by a domain that haven't swapped it for their tenant ID yet: tenant ID -> client id, from
    Microsoft's public sign-in metadata, so a GDAP customer already added by its domain isn't offered again."""
    rows = conn.execute(
        "SELECT id, tenant_ref FROM tenants WHERE tenant_ref LIKE '%.%' AND cloud = ? ORDER BY id LIMIT ?",
        (clouds.get(cloud).key, LOOKUPS),
    ).fetchall()
    found = {}
    for r in rows:
        tid = tenant_id_for_domain(r["tenant_ref"], session=session, cloud=cloud)
        if tid:
            found[tid.lower()] = r["id"]
    return found


def store_found(customers: list[Customer], domains: dict[str, int]) -> str:
    """What's kept with the sign-in row until it's used: the customers, and clients known by a domain."""
    return json.dumps({"customers": [asdict(c) for c in customers], "domains": domains})


def load_found(text: str | None) -> tuple[list[Customer], dict[str, int]]:
    try:
        data = json.loads(text or "")
    except ValueError:
        return [], {}
    if not isinstance(data, dict):
        return [], {}
    customers = []
    for c in data.get("customers") or []:
        try:
            customers.append(Customer(**c))
        except TypeError:
            continue
    domains = {str(k): int(v) for k, v in (data.get("domains") or {}).items() if str(v).isdigit()}
    return customers, domains
