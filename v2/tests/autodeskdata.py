"""Autodesk in tests: setting it up, and Autodesk's Business Success Plan Reporting API simulated at the HTTP level
with ``responses`` (the app's token, the teams a sign-in administers, exports and the files they make).

The files are shaped like the CSV exports Autodesk documents (names and addresses made up). Alpha's team is named
the old way (``offering_name`` and so on) and its users file the 2026 way ("Product", "Email"), so both are read.
Its people match the Microsoft 365 accounts ``tests/graphdata.py`` seeds: ann@x.com can sign in, bob@x.com is
blocked, and gone@x.com has no account. Dates are counted back from today, so what's recent stays recent.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta

import responses

from officesentry import store
from officesentry.autodesk import api, setup
from officesentry.db import utcnow
from officesentry.graph import GraphClient
from officesentry.worker import execute_run

CLIENT_ID = "AdskApp0123456789abcdefABCDEF01"
SECRET = "AdskAppSecretXq81zzPq0LmNv7"
PAT = "AdskPersonalTokenK29fjQ7pLx0z"
APP_TOKEN = "AdskBearerToken55kkPP0zzQ"
TEAM_A = "100200300"
TEAM_B = "400500600"
TEAM_C = "700800900"  # a Standard plan team: Autodesk lists it, but answers no export for it
FILES = "https://bsp-reporting-exports.s3.amazonaws.com"
LABEL = "Sam at the MSP"


def context(team_id: str, name: str, api_access: bool = True) -> dict:
    """One team as ``GET contexts`` lists it."""
    return {
        "contextId": team_id,
        "alias": name,
        "reportingBenefits": [setup.REPORTING, "ReportingAdvanced", "ReportingSeatUsage"]
        if api_access
        else ["ReportingSeatUsage"],
    }


CONTEXTS = [
    context(TEAM_A, "Alpha Design"),
    context(TEAM_B, "Zebedee Architects"),
    context(TEAM_C, "Charlie Standard", api_access=False),
]


def _days_ago(n: int) -> date:
    return datetime.now(UTC).date() - timedelta(days=n)


def csv_file(*lines: str) -> bytes:
    """A CSV file as Autodesk writes one: a byte order mark, then the lines."""
    return ("\ufeff" + "\r\n".join(lines) + "\r\n").encode()


def alpha_files() -> dict[str, bytes]:
    """Alpha Design: AutoCAD (5 seats, 2 held) and Revit (2 seats, both held). Ann uses both; Bob (blocked in
    Microsoft 365) hasn't opened AutoCAD for months; gone@x.com holds Revit and has no account. A row of another
    team is in each file, as an export of the wrong team would have, and must be left out."""
    recent, old, earlier = _days_ago(4), _days_ago(170), _days_ago(260)
    ends = earlier + timedelta(days=365)
    us = lambda d: d.strftime("%m/%d/%Y")  # noqa: E731 (the users file writes US dates)
    return {
        "SUBSCRIPTIONS": csv_file(
            "team_id,team_alias,subscription_id,offering_name,seat_quantity,seats_assigned,subscription_status,"
            "start_date,end_date,term",
            f"{TEAM_A},Alpha Design,110000001,AutoCAD,5,2,ACTIVE,{earlier},{ends},Annual",
            f"{TEAM_A},Alpha Design,110000002,Revit,2,2,ACTIVE,{earlier},{ends},Annual",
            f"{TEAM_B},Zebedee Architects,990000001,Maya,9,9,ACTIVE,{earlier},{ends},Annual",
        ),
        "USERS": csv_file(
            "Team ID,Team,Email,First name,Last name,Product,Subscription ID,Assigned date,Status,Last login,Country",
            f"{TEAM_A},Alpha Design,Ann@x.com,Ann,Archer,AutoCAD,110000001,{us(earlier)},Active,{us(recent)},GB",
            f"{TEAM_A},Alpha Design,ann@x.com,Ann,Archer,Revit,110000002,{us(earlier)},Active,{us(recent)},GB",
            f"{TEAM_A},Alpha Design,bob@x.com,Bob,Baker,AutoCAD,110000001,{us(earlier)},Active,{us(old)},GB",
            f"{TEAM_A},Alpha Design,gone@x.com,Gail,Gone,Revit,110000002,{us(earlier)},Active,,GB",
            f"{TEAM_B},Zebedee Architects,lee@x.com,Lee,Zebedee,Maya,990000001,,Active,,GB",
        ),
        # Two rows for Ann's AutoCAD, one per version she uses.
        "USAGE_REPORT": csv_file(
            "team_id,email,first_name,last_name,product_name,assigned_date,last_accessed,days_used,seat_assignment",
            f"{TEAM_A},ann@x.com,Ann,Archer,AutoCAD,{earlier},{recent},40,Assigned",
            f"{TEAM_A},ann@x.com,Ann,Archer,AutoCAD,{earlier},{recent - timedelta(days=2)},12,Assigned",
            f"{TEAM_A},ann@x.com,Ann,Archer,Revit,{earlier},{recent - timedelta(days=20)},20,Assigned",
            f"{TEAM_A},bob@x.com,Bob,Baker,AutoCAD,{earlier},{old},3,Assigned",
            f"{TEAM_A},carl@x.com,Carl,Clark,Inventor,,{recent},1,Unassigned",  # tried it, holds no seat
            f"{TEAM_B},lee@x.com,Lee,Zebedee,Maya,{earlier},{recent},30,Assigned",
        ),
    }


def bravo_files(mark: str = "Zebedee") -> dict[str, bytes]:
    """Bravo's team: Maya, 3 seats, one held by Lee ``mark``."""
    recent = _days_ago(2)
    return {
        "SUBSCRIPTIONS": csv_file(
            "Team ID,Team,Subscription ID,Product,Seats,Assigned seats,Status,End date",
            f"{TEAM_B},{mark} Architects,990000001,Maya,3,1,Active,{recent + timedelta(days=100)}",
        ),
        "USERS": csv_file(
            "Team ID,Email,First name,Last name,Product,Status", f"{TEAM_B},lee@x.com,Lee,{mark},Maya,Active"
        ),
        "USAGE_REPORT": csv_file(
            "Team ID,Email,Full name,Product,Last accessed,Days used", f"{TEAM_B},lee@x.com,Lee {mark},Maya,{recent},30"
        ),
    }


class Autodesk:
    """Autodesk's Reporting API for the teams in ``files`` (each team's files by report). Each export is still
    being prepared when first asked about, and ready the next time. ``usage`` is what a usage export ends as;
    ``status`` is what every call but the token request answers instead (401: the token was deleted)."""

    def __init__(self, files: dict[str, dict[str, bytes]], contexts=None, usage: str = "COMPLETED", status: int = 200):
        self.files = files
        self.contexts = CONTEXTS if contexts is None else contexts
        self.usage = usage
        self.status = status
        self.exports: dict[str, dict] = {}

    def mock(self, rsps) -> None:
        rsps.add(
            responses.POST,
            api.HOST + api.TOKEN,
            json={"access_token": APP_TOKEN, "token_type": "Bearer", "expires_in": 3599},
        )
        rsps.add_callback(responses.GET, api.HOST + "/insights/v1/contexts", callback=self._contexts)
        rsps.add_callback(responses.POST, api.HOST + api.EXPORTS, callback=self._start)
        rsps.add_callback(
            responses.GET, re.compile(re.escape(api.HOST) + r"/insights/v1/exports/[^/?]+$"), callback=self._state
        )
        rsps.add_callback(responses.GET, re.compile(re.escape(FILES) + r"/.+"), callback=self._file)

    def _refused(self, request):
        if request.headers.get("Authorization") != f"Bearer {APP_TOKEN}" or request.headers.get("ADSK-PAT") != PAT:
            return 401, {}, json.dumps({"developerMessage": "The token is not valid", "errorCode": "AUTH-001"})
        if self.status != 200:
            return self.status, {}, json.dumps({"developerMessage": "Refused", "errorCode": f"E{self.status}"})
        return None

    def _contexts(self, request):
        return self._refused(request) or (200, {}, json.dumps(self.contexts))

    def _start(self, request):
        if refused := self._refused(request):
            return refused
        body = json.loads(request.body)
        if body.get("teamId") not in self.files:
            return 403, {}, json.dumps({"developerMessage": "No access to this team", "errorCode": "AUTH-012"})
        export_id = f"exp-{len(self.exports) + 1}"
        self.exports[export_id] = {**body, "polls": 0}
        return 202, {}, json.dumps({"id": export_id, "status": "SUBMITTED"})

    def _state(self, request):
        if refused := self._refused(request):
            return refused
        export_id = request.url.rsplit("/", 1)[1]
        e = self.exports.get(export_id)
        if e is None:
            return 404, {}, json.dumps({"developerMessage": "Not found"})
        e["polls"] += 1
        status = "SUBMITTED" if e["polls"] < 2 else (self.usage if "USAGE_REPORT" in e["reports"] else "COMPLETED")
        state = {"id": export_id, "teamId": e["teamId"], "status": status}
        if status == "COMPLETED":
            state["downloads"] = [
                {
                    "type": "USAGE_REPORT_BY_PRODUCT" if r == "USAGE_REPORT" else r,
                    "downloadURL": f"{FILES}/{export_id}/{r}.csv?X-Amz-Expires=300&X-Amz-Signature=5ig",
                }
                for r in e["reports"]
            ]
        return 200, {}, json.dumps(state)

    def _file(self, request):
        export_id, name = request.url.split("?")[0].removeprefix(FILES + "/").split("/")
        e = self.exports[export_id]
        return 200, {"Content-Type": "text/csv"}, self.files[e["teamId"]][name.removesuffix(".csv")]


def set_up(db, vault, contexts=None, user: str = "admin", label: str = LABEL) -> int:
    """Autodesk's app saved and one sign-in added, checked as administering ``contexts`` (Alpha's, Bravo's and a
    Standard plan team). Returns the sign-in's id."""
    setup.save_app(db, vault, CLIENT_ID, SECRET, user)
    sign_in = setup.add_sign_in(db, vault, label, PAT, user)
    teams = [
        {"id": c["contextId"], "name": c["alias"], "benefits": sorted(c["reportingBenefits"])}
        for c in (CONTEXTS if contexts is None else contexts)
    ]
    db.execute(
        "UPDATE autodesk_sign_ins SET teams = ?, checked_at = ? WHERE id = ?", (json.dumps(teams), utcnow(), sign_in)
    )
    return sign_in


def give(db, tenant_id: int, team_id: str, user: str = "admin") -> None:
    setup.assign(db, store.get_tenant(db, tenant_id), team_id, user)


def everywhere(db, vault) -> None:
    """Autodesk set up and a team given to every client there is, for tests that open every report (its report
    is only there for a client with a team). Nothing is read, so each report says so."""
    tenants = [t["id"] for t in db.execute("SELECT id FROM tenants ORDER BY id").fetchall()]
    set_up(db, vault, [context(f"9{t:08d}", f"Team {t}") for t in tenants])
    for t in tenants:
        give(db, t, f"9{t:08d}")


def collect(db, vault, tenant_id: int, autodesk: Autodesk):
    """Run the Autodesk collection for one client, as the worker would. Returns the run's status and every
    request sent."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        autodesk.mock(rsps)
        store.enqueue_run(db, tenant_id, setup.COLLECTOR)
        run = store.claim_next_run(db)
        graph = lambda t, c: GraphClient(lambda: "t", sleep=lambda s: None)  # noqa: E731
        factory = lambda conn, sign_in: setup.client_for(conn, vault, sign_in, sleep=lambda s: None)  # noqa: E731
        status = execute_run(db, run, graph, autodesk_factory=factory)
        return status, list(rsps.calls)


def world(db, vault, alpha: int, bravo: int, mark: str) -> None:
    """Autodesk set up with one sign-in that sees both clients' teams, each given its own, and both read: Bravo's
    team and person named with ``mark``."""
    set_up(db, vault, [context(TEAM_A, "Alpha Design"), context(TEAM_B, f"{mark} Architects")])
    give(db, alpha, TEAM_A)
    give(db, bravo, TEAM_B)
    files = {TEAM_A: alpha_files(), TEAM_B: bravo_files(mark)}
    for tenant in (alpha, bravo):
        status, _ = collect(db, vault, tenant, Autodesk(files))
        assert status == "succeeded", status
