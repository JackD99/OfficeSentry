"""Record a live run's Microsoft responses as a scrubbed test fixture.

``live-test --record FILE`` passes a ``RecordingSession`` to the Graph and
Exchange clients and wraps ``dnscheck.lookup``. Everything is kept in memory
and scrubbed before anything is written: names, email addresses, phone numbers,
IP addresses, tenant domains and IDs are replaced with consistent placeholders, so the
fixture keeps real response shapes and relationships (the same user ID in
users, roles and policies) without real data. ``tests/replay.py`` serves the
file back through ``responses`` so CI runs every collector against it.

The raw responses are never written to disk.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import requests

from . import dnscheck
from .collectors.apps import MICROSOFT_TENANTS
from .exchange import EXCHANGE_ROOT
from .graph import GRAPH_ROOT

GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
EMAIL = re.compile(r"[A-Za-z0-9._%+'{}#-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}")
PHONE = re.compile(r"\+\d[\d ()-]{6,}\d")
FAKE_DOMAIN = "sentrytest"
# Lists this long are cut down: the shape matters, not 90 days of the same thing.
LIST_LIMIT = {"/v1.0/security/secureScores": 5}
# Keys that name people or devices wherever they appear in authentication methods.
DEVICE_KEYS = {"displayName", "model", "deviceTag", "phoneAppVersion", "phoneNumber", "emailAddress"}


# IDs that Office Sentry's own code looks for (role templates, guest access levels, Microsoft
# apps) are Microsoft's constants, not the tenant's data, so they're kept as they are.
KNOWN_IDS = {g.lower() for f in Path(__file__).parent.rglob("*.py")
             for g in GUID.findall(f.read_text(encoding="utf-8"))}
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")
# Fields that hold someone's IP address (audit logs, sign-ins). Version numbers look like IPs,
# so only values under these keys are replaced, with documentation addresses.
IP_KEYS = {"ipaddress", "clientip", "clientipaddress"}
IP = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}|[0-9A-Fa-f:]*:[0-9A-Fa-f:]+")


def request_key(url: str) -> tuple[str, list[tuple[str, str]]]:
    """Path and sorted query, with times blanked so "since 30 days ago" filters replay on any day."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    return parts.path, sorted((k, TIMESTAMP.sub("<time>", v)) for k, v in query)


class RecordingSession(requests.Session):
    """A requests session that keeps every Graph GET and Exchange InvokeCommand response."""

    def __init__(self, log: list):
        super().__init__()
        self.log = log

    def request(self, method, url, *args, **kwargs):
        resp = super().request(method, url, *args, **kwargs)
        try:
            body = resp.json() if resp.content else None
        except ValueError:
            body = None
        entry = {"status": resp.status_code, "body": body}
        retry = resp.headers.get("Retry-After")
        if resp.status_code == 429 or resp.status_code >= 500:
            return resp  # transient; the client retries and the retry is recorded
        if url.startswith(GRAPH_ROOT):
            entry["api"] = "graph"
            entry["url"] = resp.request.url
        elif url.startswith(EXCHANGE_ROOT):
            entry["api"] = "exchange"
            entry["url"] = url
            entry["cmdlet"] = (kwargs.get("json") or {}).get("CmdletInput", {})
        else:
            return resp
        if retry:
            entry["retry_after"] = retry
        self.log.append(entry)
        return resp


def recording_lookup(log: list, real=None):
    real = real or dnscheck.lookup

    def lookup(name: str, rdtype: str) -> list[str]:
        answer = real(name, rdtype)
        log.append({"api": "dns", "name": name, "rdtype": rdtype, "answer": answer})
        return answer
    return lookup


class Scrubber:
    """Consistent replacements for everything that could identify the tenant or its people."""

    def __init__(self, tenant_id: str, domains: list[str], names: dict[str, str]):
        self.guids: dict[str, str] = {}
        self.tenant_id = tenant_id.lower()
        self.guid(self.tenant_id)
        self.strings: dict[str, str] = {}
        self.emails: dict[str, str] = {}
        for i, domain in enumerate(sorted(set(d.lower() for d in domains), key=len, reverse=True)):
            if domain.endswith(".onmicrosoft.com"):
                fake = f"{FAKE_DOMAIN}{i or ''}.onmicrosoft.com"
            else:
                fake = f"{FAKE_DOMAIN}{i or ''}.example"
            self.strings[domain] = fake
            self.strings[domain.replace(".", "-")] = fake.replace(".", "-")
            label = domain.split(".")[0]
            if len(label) >= 4:
                self.strings[label] = fake.split(".")[0]
        self.domain_strings = list(self.strings)
        self.strings.update({k: v for k, v in names.items() if len(k) >= 3})

    def guid(self, value: str) -> str:
        low = value.lower()
        if low in MICROSOFT_TENANTS or low in KNOWN_IDS or low.startswith("00000"):
            return value  # Microsoft's own tenants and well-known IDs aren't anyone's data
        if low not in self.guids:
            n = len(self.guids) + 1
            self.guids[low] = f"5e{n:06x}-0000-4000-8000-{n:012x}"
        return self.guids[low]

    def email(self, match: re.Match) -> str:
        value = match.group(0)
        local, _, domain = value.rpartition("@")
        if domain.lower() in ("microsoft.com", "outlook.com") and local.lower().startswith(("noreply", "no-reply")):
            return value
        key = value.lower()
        if key not in self.emails:
            fake_domain = self.text_only(domain.lower())
            if fake_domain == domain.lower():
                fake_domain = "external.example"
            self.emails[key] = f"user{len(self.emails) + 1}@{fake_domain}"
        return self.emails[key]

    def text_only(self, value: str) -> str:
        for real in sorted(self.strings, key=len, reverse=True):
            if real.lower() in value.lower():
                value = _word(real).sub(self.strings[real], value)
        return value

    def text(self, value: str) -> str:
        value = EMAIL.sub(self.email, value)
        value = GUID.sub(lambda m: self.guid(m.group(0)), value)
        if value.startswith("https://"):
            # Request and paging URLs carry IDs and domains, not names; a person called "admin"
            # mustn't rewrite /admin/sharepoint/settings.
            return self._domains_only(value)
        value = PHONE.sub("+1 555 0100", value)
        return self.text_only(value)

    def _domains_only(self, value: str) -> str:
        for real in sorted(self.domain_strings, key=len, reverse=True):
            value = _word(real).sub(self.strings[real], value)
        return value

    def walk(self, value, device: bool = False):
        if isinstance(value, dict):
            return {k: ("redacted" if device and k in DEVICE_KEYS and isinstance(v, str) else self.walk(v, device))
                    for k, v in value.items()}
        if isinstance(value, list):
            return [self.walk(v, device) for v in value]
        if isinstance(value, str):
            return self.text(value)
        return value


def _word(value: str) -> re.Pattern:
    """``value`` as a whole word, so the alias "admin" doesn't turn "Administrator" into nonsense."""
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])", re.I)


def _objects(value):
    """Every dict inside a response, however deeply nested (e.g. ``$expand=principal``)."""
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from _objects(v)
    elif isinstance(value, list):
        for v in value:
            yield from _objects(v)


def _collect_names(log: list) -> tuple[list[str], dict[str, str], set[str]]:
    """Tenant domains, display names to replace (people, groups, mailboxes, the tenant's own
    apps), and the personal words (first names, surnames, nicknames) that must not survive."""
    domains: list[str] = []
    names: dict[str, str] = {}
    personal: set[str] = set()
    people = groups = apps = 0
    tenant_ids = set()
    for e in log:
        if e["api"] != "graph":
            continue
        for item in _objects(e.get("body")):
            if "userPrincipalName" not in item and "userDisplayName" not in item:
                continue
            display = item.get("displayName") or item.get("userDisplayName")
            if display and display not in names:
                people += 1
                names[display] = f"Test User {people}"
            for key in ("givenName", "surname", "mailNickname"):
                if item.get(key) and len(item[key]) >= 3:
                    names.setdefault(item[key], f"Name{people}")
                    personal.add(item[key])
            upn = (item.get("userPrincipalName") or "").split("@")[0].split("#")[0]
            if len(upn) >= 3:
                names.setdefault(upn, f"user{people}")
                personal.add(upn)
    for e in log:
        body = e.get("body") or {}
        if e["api"] != "graph" or not isinstance(body, dict):
            continue
        path = urlsplit(e["url"]).path
        items = body.get("value") if isinstance(body.get("value"), list) else [body]
        for item in items:
            if not isinstance(item, dict):
                continue
            if path.endswith("/organization"):
                tenant_ids.add((item.get("id") or "").lower())
                domains += [d["name"] for d in item.get("verifiedDomains") or [] if d.get("name")]
                if item.get("displayName"):
                    names[item["displayName"]] = "Sentry Test Ltd"
    for e in log:
        body = e.get("body") or {}
        if e["api"] != "graph" or not isinstance(body, dict):
            continue
        path = urlsplit(e["url"]).path
        for item in body.get("value") if isinstance(body.get("value"), list) else [body]:
            if not isinstance(item, dict) or not item.get("displayName"):
                continue
            if "/groups/" in path and path.count("/") == 3 and item["displayName"] not in names:
                groups += 1
                names[item["displayName"]] = f"Test Group {groups}"
            elif path.endswith(("/servicePrincipals", "/applications")) and \
                    (item.get("appOwnerOrganizationId") or "").lower() in tenant_ids | {""} and \
                    item.get("servicePrincipalType", "Application") == "Application" and \
                    item["displayName"] not in names and \
                    (item.get("appOwnerOrganizationId") or path.endswith("/applications")):
                apps += 1
                names[item["displayName"]] = f"Tenant App {apps}"
    for e in log:  # Exchange display names and aliases for the same mailboxes
        if e["api"] != "exchange" or e["cmdlet"].get("CmdletName") != "Get-Mailbox":
            continue
        for m in (e.get("body") or {}).get("value") or []:
            for key in ("DisplayName", "Name", "Alias"):
                v = m.get(key)
                if v and len(v) >= 3 and v not in names and not v.startswith("DiscoverySearchMailbox"):
                    people += 1
                    names[v] = f"Test Mailbox {people}"
    return domains, names, personal


def _collect_ips(log: list) -> dict[str, str]:
    """Real IP address -> a documentation address (203.0.113.0/24, 2001:db8::/32)."""
    ips: dict[str, str] = {}
    for e in log:
        for obj in _objects(e.get("body")):
            for k, v in obj.items():
                if k.lower() in IP_KEYS and isinstance(v, str) and IP.fullmatch(v) and v not in ips:
                    n = len(ips) + 1
                    ips[v] = f"2001:db8::{n:x}" if ":" in v else f"203.0.113.{n % 254 or 254}"
    return ips


def scrub(log: list, tenant_id: str, client_id: str) -> dict:
    domains, names, personal = _collect_names(log)
    ips = _collect_ips(log)
    s = Scrubber(tenant_id, domains, {**names, **ips})
    entries = []
    for e in log:
        if e["api"] == "dns":
            answer = e["answer"]
            if e["rdtype"] == "TXT":  # drop verification tokens; keep what the checks read
                answer = [a for a in answer if a.lower().startswith(("v=spf1", "v=dmarc1", "v=stsv1"))]
            entries.append({"api": "dns", "name": s.text(e["name"]), "rdtype": e["rdtype"],
                            "answer": [s.text(a) for a in answer]})
            continue
        body = e["body"]
        path = urlsplit(e["url"]).path
        if isinstance(body, dict) and isinstance(body.get("value"), list) and path in LIST_LIMIT:
            body = dict(body, value=body["value"][:LIST_LIMIT[path]])
        if path.endswith("/applications") and isinstance(body, dict):
            body = _rename_credentials(body)
        out = {"api": e["api"], "url": s.text(e["url"]), "status": e["status"],
               "body": s.walk(body, device="/authentication/methods" in path)}
        if e["api"] == "exchange":
            out["cmdlet"] = s.walk(e["cmdlet"])
        entries.append(out)
    fixture = {"tenant_id": s.guid(tenant_id.lower()), "client_id": s.guid(client_id.lower()), "entries": entries}
    # URLs are checked for IDs and domains only: a person named "admin" isn't leaked by /admin/sharepoint.
    text = "\n".join(v for v in _strings(fixture) if not v.startswith("https://"))
    leaks = find_leaks(text, [*names, *personal])
    leaks += find_leaks(json.dumps(fixture), [tenant_id, client_id, *domains, *s.emails, *ips])
    if leaks:
        hints = ", ".join(f"{v[0]}… ({len(v)} chars)" for v in leaks)  # never print the values themselves
        raise ValueError(f"scrubbing left {len(leaks)} identifying value(s) in the fixture: {hints}")
    return fixture


def _rename_credentials(body: dict) -> dict:
    """App secret and certificate names are free text people type, so they're replaced."""
    apps = []
    for app in body.get("value") or []:
        app = dict(app)
        for key in ("passwordCredentials", "keyCredentials"):
            app[key] = [dict(c, displayName=f"Credential {i + 1}" if c.get("displayName") else None)
                        for i, c in enumerate(app.get(key) or [])]
        apps.append(app)
    return dict(body, value=apps)


def _strings(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, str):
        yield value


def find_leaks(text: str, sensitive) -> list[str]:
    values = {str(v) for v in sensitive if v and len(str(v)) >= 4}
    return [v for v in values if _word(v).search(text)]
