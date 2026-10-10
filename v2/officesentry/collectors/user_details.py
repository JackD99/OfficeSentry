"""People directory: each account's department, job title, office and manager.

Read separately from Users, whose listing asks for sign-in activity: Microsoft limits that listing to 10
requests a minute for the whole app, so it stays as small as it can be, and this one pages 999 accounts at a
time. Keyed by the account's object ID, like Users, so the All users report joins the two.
"""

from __future__ import annotations

from .base import Collector, RunContext, register

FIELDS = ("id,department,jobTitle,companyName,officeLocation,city,country,employeeId,employeeType")
MANAGER = "manager($select=id,displayName,userPrincipalName,mail,accountEnabled)"
TEXT_FIELDS = ("department", "jobTitle", "companyName", "officeLocation", "city", "country", "employeeId",
               "employeeType")


def _text(value) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _manager(value) -> dict | None:
    if not isinstance(value, dict) or not value.get("id"):
        return None
    kind = (value.get("@odata.type") or "").rsplit(".", 1)[-1] or "user"
    return {"id": value["id"], "name": _text(value.get("displayName")),
            "upn": _text(value.get("userPrincipalName")) or _text(value.get("mail")), "type": kind,
            "enabled": value.get("accountEnabled") if isinstance(value.get("accountEnabled"), bool) else None}


def summarise(user: dict) -> dict:
    out = {key: _text(user.get(key)) for key in TEXT_FIELDS}
    out["manager"] = _manager(user.get("manager"))
    return out


@register
class UserDetails(Collector):
    key = "user_details"
    title = "Departments and managers"
    description = "Each account's department, job title, office, company and manager."
    permissions = ("User.Read.All",)

    def collect(self, ctx: RunContext) -> None:
        users = ctx.paged(ctx.graph.get_all("/users", params={"$select": FIELDS, "$expand": MANAGER,
                                                              "$top": "999"}))
        rows = [(u["id"], summarise(u)) for u in users if u.get("id")]
        ctx.objects("person", rows)
        data = [r for _, r in rows]
        ctx.metric("people.with_manager", sum(bool(r["manager"]) for r in data))
        ctx.metric("people.with_department", sum(bool(r["department"]) for r in data))
