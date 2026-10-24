"""Autodesk's Business Success Plan Reporting API (formerly the Premium Reporting API): how Office Sentry reads a
client's Autodesk subscriptions, who holds each seat and how much they use it.

Base address ``https://developer.api.autodesk.com/insights/v1``. It only answers for Autodesk teams on the
Business Success Plan (Premium) or Enterprise plan, and only to an admin of the team: Autodesk has no API for a
Standard plan team, and none that assigns seats or changes users.

Signing in: the MSP's own app, registered once at aps.autodesk.com and bound to the Reporting API only, gets a
token with its client ID and secret (client credentials), and every call also carries the personal access token
an admin of the team made for that app, in the ``ADSK-PAT`` header. The token and the PAT only ever go to
Autodesk's API host, and only to the paths below.

Reading:

* ``contexts()``: the teams the sign-in administers, with their reporting benefits (``GET contexts``).
* ``start_export()`` then ``wait()``: Autodesk prepares a report file (``POST exports``, then ``GET
  exports/<id>`` until it's done), and ``download()`` fetches it from the short-lived link Autodesk gives. The
  link is pre-signed, so nothing secret is sent with it.

The two calls that aren't GETs are here and nowhere else (``tests/test_no_writes.py`` names this file): the
token request, and ``POST exports``, which asks Autodesk to prepare a report file. Neither changes anything in
the client's Autodesk account: no user, seat, team or setting. An export always names one team; Autodesk would
otherwise export every team the sign-in administers, which may be several clients'. 429 and 5xx are retried
after ``Retry-After`` (Autodesk allows 10 export requests a minute per person, and 10 running at once), and
every request is counted in ``telemetry`` for the run page.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from collections.abc import Callable
from datetime import datetime
from urllib.parse import urlsplit

import requests

from ..graph import USER_AGENT
from ..ratelimit import Telemetry

log = logging.getLogger(__name__)

HOST = "https://developer.api.autodesk.com"
TOKEN = "/authentication/v2/token"  # noqa: S105 (the token request's path, not a secret)
SCOPES = "data:read data:write"  # Autodesk asks for data:write on POST exports, though it only prepares a file
# Every path a token or PAT may go to, besides the token request itself.
READS = (re.compile(r"/insights/v1/contexts"), re.compile(r"/insights/v1/exports/[A-Za-z0-9-]{1,64}"))
EXPORTS = "/insights/v1/exports"
REPORTS = ("SUBSCRIPTIONS", "USERS", "USAGE_REPORT")
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER = 300
MAX_DOWNLOAD = 50 * 1024 * 1024  # an export file bigger than this isn't a team's seats
WAIT_FOR = 15 * 60  # how long ``wait`` gives Autodesk to prepare an export
POLL = (5, 10, 20, 30)  # seconds between checks on an export, the last repeated
TEAM_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CLIENT_ID = re.compile(r"^[A-Za-z0-9]{8,64}$")


class AutodeskError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"Autodesk {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class AutodeskAccessDenied(AutodeskError):
    """Autodesk refused the app's client ID and secret, the personal access token, or the team: words for people
    are in ``refused()``."""


class ExportFailed(AutodeskError):
    """Autodesk couldn't prepare an export, or took longer than ``WAIT_FOR``."""


def refused(exc: AutodeskError) -> str:
    """Why Autodesk said no, in words for the person who set it up."""
    if exc.code == "invalid_client" or (exc.status in (400, 401) and exc.code.startswith("token")):
        return (
            "Autodesk refused the app's client ID or secret. Check them on Settings, Autodesk (the secret is shown "
            "once on aps.autodesk.com, under My Apps)."
        )
    if exc.status == 401:
        return (
            "Autodesk refused the personal access token: it was deleted, or it was made for another app. Make a new "
            "one for this app and enter it again."
        )
    if exc.status == 403:
        return (
            "Autodesk says this sign-in isn't an admin of a team with API reporting. The Reporting API only answers "
            "an admin of a team on the Business Success Plan (formerly Premium) or Enterprise plan."
        )
    return f"Autodesk answered {exc.status}: {exc.message[:200]}"


def _parse_error(resp: requests.Response, token: bool = False) -> AutodeskError:
    code, message = str(resp.status_code), resp.text[:300] or resp.reason or ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            code = str(data.get("errorCode") or data.get("error") or data.get("title") or code)
            said = data.get("developerMessage") or data.get("error_description") or data.get("detail")
            message = str(said or message)
    except ValueError:
        pass
    if token:
        code = code if code == "invalid_client" else f"token_{code}"
    if resp.status_code in (401, 403) or (token and resp.status_code == 400):
        return AutodeskAccessDenied(resp.status_code, code, message)
    return AutodeskError(resp.status_code, code, message)


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class AutodeskClient:
    """One Autodesk sign-in: the MSP's app (``client_id``, ``client_secret``) and an admin's personal access
    token (``pat``) made for that app."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        pat: str,
        *,
        session: requests.Session | None = None,
        max_retries: int = 4,
        timeout: int = 60,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        telemetry: Telemetry | None = None,
    ):
        if not (client_id and client_secret and pat):
            raise ValueError("an Autodesk sign-in needs the app's client ID and secret and a personal access token")
        self._client_id = client_id
        self._client_secret = client_secret
        self._pat = pat
        self._session = session or requests.Session()
        self._max_retries = max_retries
        self._timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self.telemetry = telemetry or Telemetry()
        self._token: tuple[str, float] | None = None

    # -- Sending ------------------------------------------------------------------------------------------

    def _attempts(self, key: str, send: Callable[[], requests.Response]) -> requests.Response:
        """``send`` until it's answered with something other than a 429 or 5xx, or the retries run out."""
        attempt = 0
        while True:
            try:
                resp = send()
            except (requests.ConnectionError, requests.Timeout) as exc:
                retry = attempt < self._max_retries
                delay = float(min(2**attempt, 60)) if retry else 0.0
                self.telemetry.request(key, None, status=0, retry_wait=delay)
                if not retry:
                    raise AutodeskError(0, "network_error", str(exc)) from exc
                self._sleep(delay)
                attempt += 1
                continue
            retry = resp.status_code in RETRY_STATUSES and attempt < self._max_retries
            delay = 0.0
            if retry:
                value = resp.headers.get("Retry-After", "").strip()
                delay = min(float(value), MAX_RETRY_AFTER) if value.isdigit() else float(min(2**attempt, 60))
            self.telemetry.request(key, None, status=resp.status_code, retry_wait=delay)
            if not retry:
                return resp
            log.info("Autodesk %s on %s; retrying in %ss", resp.status_code, key, delay)
            self._sleep(delay)
            attempt += 1

    def _bearer(self) -> str:
        """The app's own token (client credentials), kept until a minute before it runs out."""
        if self._token and self._clock() < self._token[1]:
            return self._token[0]
        basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        resp = self._attempts(
            "Autodesk token",
            lambda: self._session.post(
                HOST + TOKEN,
                data={"grant_type": "client_credentials", "scope": SCOPES},
                headers={"Authorization": f"Basic {basic}", "User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=self._timeout,
            ),
        )
        if not resp.ok:
            raise _parse_error(resp, token=True)
        data = self._json(resp)
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise AutodeskError(resp.status_code, "no_token", "Autodesk's answer had no access token")
        try:
            lasts = float(data.get("expires_in") or 3600)
        except (TypeError, ValueError):
            lasts = 3600.0
        self._token = (token, self._clock() + max(lasts - 60, 60))
        return token

    def _send(self, method: str, path: str, body: dict | None = None) -> requests.Response:
        allowed = any(p.fullmatch(path) for p in READS) if method == "GET" else (method, path) == ("POST", EXPORTS)
        if not allowed:
            raise ValueError(f"refusing to send an Autodesk token to {method} {path[:80]}")
        url = HOST + path
        key = f"Autodesk {method} {re.sub(r'/exports/.+', '/exports/{id}', path)}"

        def send() -> requests.Response:
            headers = {
                "Authorization": f"Bearer {self._bearer()}",
                "ADSK-PAT": self._pat,
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            }
            if method == "POST":
                return self._session.post(url, json=body, headers=headers, timeout=self._timeout)
            return self._session.get(url, headers=headers, timeout=self._timeout)

        resp = self._attempts(key, send)
        if not resp.ok:
            raise _parse_error(resp)
        return resp

    @staticmethod
    def _json(resp: requests.Response):
        try:
            return resp.json() if resp.content else None
        except ValueError as exc:
            raise AutodeskError(resp.status_code, "bad_json", "Autodesk's answer wasn't JSON") from exc

    # -- Reading ------------------------------------------------------------------------------------------

    def contexts(self) -> list[dict]:
        """The teams this sign-in administers: [{"id", "name", "benefits"}]."""
        data = self._json(self._send("GET", "/insights/v1/contexts"))
        out = []
        for c in data if isinstance(data, list) else []:
            if not isinstance(c, dict) or not TEAM_ID.match(str(c.get("contextId") or "")):
                continue
            benefits = c.get("reportingBenefits")
            out.append(
                {
                    "id": str(c["contextId"]),
                    "name": str(c.get("alias") or c["contextId"])[:200],
                    "benefits": sorted(str(b) for b in benefits if isinstance(b, str))
                    if isinstance(benefits, list)
                    else [],
                }
            )
        return out

    def start_export(
        self, team_id: str, reports: tuple[str, ...], start: datetime | None = None, end: datetime | None = None
    ) -> str:
        """Ask Autodesk to prepare one team's export as CSV files. Returns the export's ID. ``start`` and ``end``
        bound the usage report (Autodesk allows a year at most)."""
        if not TEAM_ID.match(team_id or ""):
            raise ValueError("an Autodesk export must name its team")  # else every team the sign-in sees
        if not reports or any(r not in REPORTS for r in reports):
            raise ValueError(f"Autodesk exports are {', '.join(REPORTS)}")
        body: dict = {"teamId": team_id, "outputFormat": "CSV", "reports": list(reports)}
        if "USAGE_REPORT" in reports:
            if start is None or end is None:
                raise ValueError("the usage report needs a start and an end")
            body |= {"usageReports": ["USAGE_REPORT_BY_PRODUCT"], "startDate": _stamp(start), "endDate": _stamp(end)}
        data = self._json(self._send("POST", EXPORTS, body))
        export_id = str((data or {}).get("id") or "") if isinstance(data, dict) else ""
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", export_id):
            raise AutodeskError(200, "no_export_id", "Autodesk didn't say which export it started")
        return export_id

    def export(self, export_id: str) -> dict:
        """One export's state: {"status", "teamId", "downloads": [{"type", "downloadURL"}]}."""
        data = self._json(self._send("GET", f"{EXPORTS}/{export_id}"))
        return data if isinstance(data, dict) else {}

    def wait(self, export_id: str, team_id: str, timeout: float = WAIT_FOR) -> dict[str, str]:
        """Check on an export until Autodesk has prepared it. Returns each report's download link, by report.
        Refuses an export of any team but ``team_id``."""
        started = self._clock()
        checks = 0
        while True:
            state = self.export(export_id)
            status = str(state.get("status") or "").upper()
            if state.get("teamId") not in (None, "", team_id):
                raise ExportFailed(200, "other_team", "Autodesk returned an export of another team")
            if status == "COMPLETED":
                links = {}
                for d in state.get("downloads") or []:
                    if isinstance(d, dict) and isinstance(d.get("downloadURL"), str):
                        links[str(d.get("type") or "").upper()] = d["downloadURL"]
                return links
            if status in ("ERROR", "EXPIRED"):
                raise ExportFailed(200, status.lower(), f"Autodesk couldn't prepare the export ({status.lower()})")
            if self._clock() - started > timeout:
                minutes = int(timeout // 60)
                raise ExportFailed(0, "timeout", f"Autodesk hadn't prepared the export after {minutes} minutes")
            self._sleep(POLL[min(checks, len(POLL) - 1)])
            checks += 1

    def download(self, url: str) -> bytes:
        """One prepared file, from the pre-signed link Autodesk gave for it. No token or PAT is sent with it."""
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise AutodeskError(0, "bad_link", "Autodesk's download link isn't an https address")
        if re.fullmatch(r"[\d.]+|\[.*\]", parts.netloc.split(":")[0]):
            raise AutodeskError(0, "bad_link", "Autodesk's download link is an address, not a host name")

        def send() -> requests.Response:
            return self._session.get(url, headers={"User-Agent": USER_AGENT}, timeout=self._timeout, stream=True)

        resp = self._attempts("Autodesk download", send)
        if not resp.ok:
            raise AutodeskError(resp.status_code, "download_failed", "Autodesk's export file couldn't be downloaded")
        chunks, size = [], 0
        for chunk in resp.iter_content(64 * 1024):
            size += len(chunk)
            if size > MAX_DOWNLOAD:
                resp.close()
                raise AutodeskError(200, "too_big", "Autodesk's export file is larger than 50 MB")
            chunks.append(chunk)
        return b"".join(chunks)
