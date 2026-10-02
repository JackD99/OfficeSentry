"""Robustness: every check, detail report and change feed survives degraded snapshots.

A seeded tenant is collected once (twice, so "what changed" has a baseline). Each test then
replays the stored objects through a patched ``store.run_objects`` with one kind of object
degraded at a time:

  removed / none      a top-level field is missing, or null
  as_string / as_list a list field holds a string, or a string field holds a list
  ts_*                a timestamp is "", "0001-01-01T00:00:00Z", "not-a-date" or has no "Z"
  empty               the collector returned no objects of that kind

Targets come from the registries (``rules.RULES``, ``data_reports.REPORTS`` and every
``changes.*_changes`` function), so checks and reports added later are covered automatically.
A target may report "unavailable"; it must not raise, return malformed results, or "fail"
with no findings.

Crashes in code owned by other threads are listed in ``KNOWN`` and asserted as strict xfails
in ``test_known_crashes`` so they show up as XPASS (and fail) once fixed.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from officesentry import changes, findings, store
from officesentry.certs import generate_certificate
from officesentry.config import load_config
from officesentry.crypto import Vault
from officesentry.db import connect, migrate
from officesentry.reports import data_reports, evidence, monthly_review
from officesentry.rules import RULES, SEVERITY_RANK, Finding, Rule, evaluate
from officesentry.snapshot import TenantSnapshot
from tests.conftest import login, make_user
from tests.seed import seed_tenant

_real_run_objects = store.run_objects

TIMESTAMP_NAME = re.compile(r"(DateTime|Date|^date|_at|Created)$")
TIMESTAMP_VALUE = re.compile(r"^\d{4}-\d{2}-\d{2}")
BAD_TIMESTAMPS = {"ts_empty": "", "ts_zero": "0001-01-01T00:00:00Z", "ts_garbage": "not-a-date"}
FLAGS = {None, *SEVERITY_RANK}

CHANGE_FEEDS = {name: fn for name, fn in vars(changes).items()
                if name.endswith("_changes") and name != "all_changes" and callable(fn)}


# -- Degraded snapshots ---------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    collector: str
    kind: str
    field: str  # "*" for the whole kind; "a[].b" or "a.b" for a nested timestamp
    mutation: str

    @property
    def name(self) -> str:
        return f"{self.collector}/{self.kind}.{self.field}:{self.mutation}"

    def apply(self, rows: list[dict]) -> list[dict]:
        if self.mutation == "empty":
            return []
        for row in rows:
            for holder, key in _holders(row, self.field):
                _mutate(holder, key, self.mutation)
        return rows


def _holders(row: dict, path: str):
    """The dicts (and key) a field path points at inside one object."""
    if "[]." in path:
        outer, inner = path.split("[].")
        items = row.get(outer)
        return [(i, inner) for i in items if isinstance(i, dict)] if isinstance(items, list) else []
    if "." in path:
        outer, inner = path.split(".", 1)
        return [(row[outer], inner)] if isinstance(row.get(outer), dict) else []
    return [(row, path)]


def _mutate(holder: dict, key: str, mutation: str) -> None:
    if mutation == "removed":
        holder.pop(key, None)
    elif mutation == "none":
        holder[key] = None
    elif mutation == "as_string":
        holder[key] = "unexpected text"
    elif mutation == "as_list":
        holder[key] = ["unexpected", "list"]
    elif mutation == "ts_naive":
        value = holder.get(key)
        holder[key] = value[:-1] if isinstance(value, str) and value.endswith("Z") else "2026-01-01T00:00:00"
    else:
        holder[key] = BAD_TIMESTAMPS[mutation]


def _is_timestamp(name: str, values: list) -> bool:
    return bool(TIMESTAMP_NAME.search(name)) or any(isinstance(v, str) and TIMESTAMP_VALUE.match(v) for v in values)


def build_cases(rows_by_kind: dict[tuple[str, str], list[dict]]) -> dict[tuple[str, str], list[Case]]:
    out: dict[tuple[str, str], list[Case]] = {}
    for (collector, kind), rows in sorted(rows_by_kind.items()):
        cases = [Case(collector, kind, "*", "empty")]
        values: dict[str, list] = {}
        nested: set[str] = set()
        for row in rows:
            for name, value in row.items():
                if name.startswith("_"):
                    continue
                values.setdefault(name, []).append(value)
                # Timestamps one level down, e.g. application credentials' endDateTime.
                items = value if isinstance(value, list) else [value]
                for item in items:
                    if isinstance(item, dict):
                        sep = "[]." if isinstance(value, list) else "."
                        nested.update(f"{name}{sep}{k}" for k, v in item.items() if _is_timestamp(k, [v]))
        for name, vals in sorted(values.items()):
            mutations = ["removed", "none"]
            kinds = {type(v) for v in vals if v is not None}
            if list in kinds or not kinds:
                mutations.append("as_string")
            if str in kinds or not kinds:
                mutations.append("as_list")
            if _is_timestamp(name, vals):
                mutations += [*BAD_TIMESTAMPS, "ts_naive"]
            cases += [Case(collector, kind, name, m) for m in mutations]
        for path in sorted(nested):
            cases += [Case(collector, kind, path, m) for m in (*BAD_TIMESTAMPS, "ts_naive")]
        out[(collector, kind)] = cases
    return out


class Degrader:
    """Stands in for ``store.run_objects``: replays stored objects, degrading one kind of the latest runs."""

    def __init__(self, conn, tenant_id: int):
        self.latest = {r["id"]: r["collector"] for r in conn.execute(
            "SELECT MAX(id) AS id, collector FROM runs WHERE tenant_id = ? AND status IN ('succeeded', 'partial')"
            " GROUP BY collector", (tenant_id,))}
        self.rows: dict[tuple[int, str], list[dict]] = {}
        for run in conn.execute("SELECT id FROM runs WHERE tenant_id = ?", (tenant_id,)).fetchall():
            for obj in _real_run_objects(conn, run["id"]):
                self.rows.setdefault((run["id"], obj["_kind"]), []).append(obj)
        self.case: Case | None = None
        self.reads: set[tuple[str, str]] = set()

    def latest_rows(self) -> dict[tuple[str, str], list[dict]]:
        return {(self.latest[run_id], kind): rows for (run_id, kind), rows in self.rows.items()
                if run_id in self.latest}

    def __call__(self, conn, run_id, kind=None, limit=None):
        if kind is None or limit:
            return _real_run_objects(conn, run_id, kind, limit)
        rows = copy.deepcopy(self.rows.get((run_id, kind), []))
        if run_id in self.latest:
            key = (self.latest[run_id], kind)
            self.reads.add(key)
            if self.case is not None and (self.case.collector, self.case.kind) == key:
                rows = self.case.apply(rows)
        return rows


@dataclass
class Seeded:
    conn: object
    tenant: object
    degrader: Degrader
    cases: dict[tuple[str, str], list[Case]]
    now: datetime


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        for var in ("SECRET_KEY", "ENCRYPTION_KEYS", "DATA_DIR"):
            mp.delenv("OFFICESENTRY_" + var, raising=False)
        cfg = load_config(data_dir=tmp_path_factory.mktemp("robustness"), testing=True, secure_cookies=False)
    conn = connect(cfg.db_path)
    migrate(conn)
    cid = store.create_connection(conn, Vault(cfg.encryption_keys), "Test app", generate_certificate("test"),
                                  client_id="11111111-2222-3333-4444-555555555555")
    tid = store.create_tenant(conn, "Alpha Ltd", "alpha.onmicrosoft.com", cid)
    seed_tenant(conn, tid)
    # Push the first collection back so the second one has a baseline for "what changed".
    old = (datetime.now(UTC) - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("UPDATE runs SET finished_at = ?, started_at = ?, created_at = ?", (old, old, old))
    seed_tenant(conn, tid)
    degrader = Degrader(conn, tid)
    yield Seeded(conn, store.get_tenant(conn, tid), degrader, build_cases(degrader.latest_rows()),
                 datetime.now(UTC))
    conn.close()


# -- Validating what each target returns --------------------------------------------------


def check_rule(result) -> list[str]:
    if not (isinstance(result, tuple) and len(result) == 3):
        return [f"returned {result!r}, not (status, summary, findings)"]
    status, summary, found = result
    problems = []
    if status not in ("pass", "fail", "unavailable"):
        problems.append(f"status {status!r}")
    if not isinstance(summary, str):
        problems.append(f"summary {summary!r}")
    if not isinstance(found, list):
        return problems + [f"findings {found!r}"]
    if status == "fail" and not found:
        problems.append(f"fail with no findings ({summary})")
    for f in found:
        if not isinstance(f, Finding) or f.severity not in SEVERITY_RANK or not isinstance(f.subject, str) \
                or not isinstance(f.detail, str) or not isinstance(f.object_id, (str, type(None))):
            problems.append(f"malformed finding {f!r}")
    return problems


def check_report(report: data_reports.DataReport) -> list[str]:
    if report.unavailable is not None:
        return [] if isinstance(report.unavailable, str) else [f"unavailable {report.unavailable!r}"]
    problems = []
    for s in report.sections:
        if len(s.flags) != len(s.rows):
            problems.append(f"{s.title}: {len(s.rows)} rows but {len(s.flags)} flags")
        for row, flag in zip(s.rows, s.flags, strict=False):
            if len(row) != len(s.columns):
                problems.append(f"{s.title}: row {row!r} doesn't match columns {s.columns}")
            if flag not in FLAGS:
                problems.append(f"{s.title}: flag {flag!r}")
            for cell in row:
                if not isinstance(cell, (str, int, float)):
                    problems.append(f"{s.title}: cell {cell!r} can't go in a spreadsheet")
    return problems


def check_changes(result) -> list[str]:
    if not isinstance(result, list):
        return [f"returned {result!r}"]
    return [f"malformed change {c!r}" for c in result
            if not isinstance(c, changes.Change) or not all(isinstance(v, str) for v in (c.subject, c.detail))]


def run_rule(s: Seeded, r: Rule):
    return check_rule(r.check(TenantSnapshot(s.conn, s.tenant), s.now))


def run_report(s: Seeded, key: str):
    title, description, _collectors, builder = data_reports.REPORTS[key]
    report = data_reports.DataReport(key, title, description, dict(s.tenant), s.now)
    builder(TenantSnapshot(s.conn, s.tenant), s.now, report)
    return check_report(report)


def run_changes(s: Seeded, name: str):
    since = (s.now - monthly_review.PERIOD).strftime("%Y-%m-%dT%H:%M:%SZ")
    return check_changes(CHANGE_FEEDS[name](s.conn, s.tenant["id"], since))


RUNNERS = {"rule": lambda s, key: run_rule(s, next(r for r in RULES if r.key == key)),
           "report": run_report, "changes": run_changes}


def _where(exc: Exception) -> str:
    """The innermost application function in a traceback, e.g. ``licences.py:last_sign_in``."""
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if "/officesentry/" in f.filename.replace("\\", "/")]
    if not frames:
        return "?"
    path = frames[-1].filename
    named = [f.name for f in frames if f.filename == path and not f.name.startswith("<")] or [frames[-1].name]
    return f"{path.replace(chr(92), '/').rsplit('/officesentry/', 1)[-1]}:{named[-1]}"




# -- Known crashes in code owned by other threads --------------------------------------------
# Listed in docs/HANDOFF-cloud-session.md. Each entry is a strict xfail in test_known_crashes,
# so fixing one makes it XPASS (and fail) until the entry is removed here.


@dataclass(frozen=True)
class Known:
    """Degradations one target is known to mishandle, e.g. ``"principalId|roleName:removed"``."""

    target: str
    key: str
    kind: str   # "collector/kind"
    cases: str  # space-separated "field|field:mutation|mutation" groups
    cause: str

    def matches(self, case: Case) -> bool:
        if f"{case.collector}/{case.kind}" != self.kind:
            return False
        for group in self.cases.split():
            fields, mutations = group.rsplit(":", 1)
            if case.field in fields.split("|") and case.mutation in mutations.split("|"):
                return True
        return False

    @property
    def id(self) -> str:
        return f"{self.target}-{self.key}-{self.kind}-{self.cases.replace(' ', ',')}"


LABEL = "rules.py:_label: a name that isn't text becomes the finding subject, which evaluate() sorts with .lower()"
ADMIN_KEYS = "a['principalId'] / a['roleName'] indexed directly"
SIGN_IN = ("licences.py:last_sign_in/is_stale: datetime.fromisoformat() on unchecked values; a timestamp "
           "without a zone can't be compared with an aware one")
TS_FIELDS = "createdDateTime|lastNonInteractiveSignInDateTime|lastSignInDateTime|lastSuccessfulSignInDateTime"
SIGN_IN_FIELDS = "lastNonInteractiveSignInDateTime|lastSignInDateTime|lastSuccessfulSignInDateTime"
DNS = ("dnscheck.py:DomainHealth.from_dict/issues/microsoft_365: assume every stored field is present and typed "
       "(None compared with >, dkim.get on None, iterating mx)")
DNS_SUBJECT = "rules.py:_domain_rule: the domain name is used as the finding subject and a set member unchecked"
INBOX = "mailflow.py:inbox_rule_risk: regex and .lower() on displayName / moveToFolderName without a type check"
DOMAINS = "mailflow.py:tenant_domains: accepted domain names put in a set unchecked"
FORWARDING = ("mailflow.py:mailbox_forwarding/smtp_address: ForwardingRecipient assumed to be a dict and "
              "ForwardingSmtpAddress text")
APP_RISKS = "rules.py:app_risks: permission / principalId used as set members, displayName sorted with .lower()"
DIRECTORY = "coverage.py:directory: r['principalId'] indexed directly and role IDs used as dict keys unchecked"

KNOWN: list[Known] = [
    # rules.py: identity, admin and licence checks
    Known("rule", "mfa_enforced", "conditional_access/ca_policy", "displayName:none|as_list",
          "rules.py:mfa_enforced: \", \".join() over policy displayName values that can be null"),
    Known("rule", "mfa_enforced", "admin_roles/role_assignment", "principalId:as_list",
          "rules.py:_names: principalId used as a dict key unchecked"),
    Known("rule", "mfa_enforced", "mfa_registration/mfa_registration", "userPrincipalName:as_list", LABEL),
    Known("rule", "admin_mfa", "admin_roles/role_assignment", "principalId|roleName:removed",
          f"rules.py:admin_mfa: {ADMIN_KEYS}"),
    Known("rule", "admin_mfa", "admin_roles/role_assignment", "principalId|userPrincipalName:as_list",
          f"rules.py:admin_mfa: principalId used as a set member unchecked; {LABEL}"),
    Known("rule", "admin_mfa", "mfa_registration/mfa_registration", "isMfaRegistered:removed",
          "rules.py:admin_mfa: r['isMfaRegistered'] indexed directly"),
    Known("rule", "global_admin_count", "admin_roles/role_assignment", "principalId:removed|as_list",
          "rules.py:global_admin_count: a['principalId'] indexed directly and used as a dict key"),
    Known("rule", "guest_admins", "admin_roles/role_assignment", "principalId|roleName:removed",
          f"rules.py:guest_admins: {ADMIN_KEYS}"),
    Known("rule", "guest_admins", "admin_roles/role_assignment", "principalId|userPrincipalName:as_list",
          f"rules.py:guest_admins: principalId passed as the finding's object_id unchecked; {LABEL}"),
    Known("rule", "service_principal_admins", "admin_roles/role_assignment", "principalId|roleName:removed",
          f"rules.py:service_principal_admins: {ADMIN_KEYS}"),
    Known("rule", "service_principal_admins", "admin_roles/role_assignment", "displayName|principalId:as_list",
          "rules.py:service_principal_admins: displayName / principalId passed into the finding unchecked"),
    Known("rule", "users_mfa_registered", "mfa_registration/mfa_registration", "isMfaRegistered:removed",
          "rules.py:users_mfa_registered: r['isMfaRegistered'] indexed directly"),
    Known("rule", "users_mfa_registered", "mfa_registration/mfa_registration", "userPrincipalName|userType:as_list",
          f"rules.py:users_mfa_registered: .lower() on userType unchecked; {LABEL}"),
    Known("rule", "stale_users", "users/user", f"{TS_FIELDS}:as_string|as_list|ts_garbage|ts_naive", SIGN_IN),
    Known("rule", "stale_users", "users/user", "userPrincipalName:as_list", LABEL),
    Known("rule", "licensed_disabled", "users/user", "userPrincipalName:as_list", LABEL),
    Known("rule", "unassigned_licences", "tenant_profile/subscribed_sku",
          "consumedUnits:none skuId:removed|as_list skuPartNumber:as_list",
          "licences.py:licence_summary/sku_name: sku['skuId'] indexed directly, consumedUnits used in arithmetic "
          "and SKU IDs used as dict keys unchecked"),
    Known("rule", "admins_ca_mfa", "admin_roles/role_assignment",
          "principalId:removed|as_list roleDefinitionId:as_list", DIRECTORY),
    Known("rule", "admins_ca_mfa", "admin_roles/role_definition", "templateId:as_list", DIRECTORY),
    Known("rule", "admins_ca_mfa", "admin_roles/role_assignment", "roleName:removed userPrincipalName:as_list",
          f"rules.py:admins_ca_mfa: a['roleName'] indexed directly; {LABEL}"),
    Known("rule", "admins_ca_mfa", "conditional_access/ca_policy", "displayName:as_list",
          "rules.py:admins_ca_mfa: \", \".join() over policy names that aren't text"),
    # rules.py: email checks (and mailflow.py, which they share with the Mailboxes report)
    Known("rule", "external_forwarding", "inbox_rules/inbox_rule", "displayName|moveToFolderName:as_list", INBOX),
    Known("rule", "external_forwarding", "inbox_rules/inbox_rule", "mailbox:as_list",
          "rules.py:external_forwarding: the rule's mailbox is the finding subject, unchecked"),
    Known("rule", "external_forwarding", "mailboxes/accepted_domain", "DomainName:as_list", DOMAINS),
    Known("rule", "external_forwarding", "mailboxes/mailbox",
          "ForwardingRecipient:as_string|as_list ForwardingSmtpAddress:as_list", FORWARDING),
    Known("rule", "external_forwarding", "mailboxes/mailbox", "PrimarySmtpAddress:as_list",
          "rules.py:external_forwarding: PrimarySmtpAddress is the finding subject, unchecked"),
    Known("rule", "suspicious_inbox_rules", "inbox_rules/inbox_rule", "displayName|moveToFolderName:as_list", INBOX),
    Known("rule", "suspicious_inbox_rules", "inbox_rules/inbox_rule", "mailbox:as_list",
          "rules.py:suspicious_inbox_rules: the rule's mailbox is the finding subject, unchecked"),
    Known("rule", "suspicious_inbox_rules", "mailboxes/accepted_domain", "DomainName:as_list", DOMAINS),
    Known("rule", "shared_mailbox_signin", "mailboxes/mailbox", "ExternalDirectoryObjectId:as_list",
          "rules.py:shared_mailbox_signin: ExternalDirectoryObjectId tested for set membership unchecked"),
    # rules.py: apps and sharing
    Known("rule", "risky_app_permissions", "app_consents/app_permission", "permission:as_list", APP_RISKS),
    Known("rule", "risky_app_permissions", "app_consents/delegated_grant", "principalId:as_list", APP_RISKS),
    Known("rule", "risky_app_permissions", "app_consents/service_principal", "displayName:as_list", APP_RISKS),
    Known("rule", "app_credentials", "app_consents/application",
          "credentials:as_string credentials[].endDateTime:ts_garbage displayName:as_list",
          "rules.py:app_credentials: credentials assumed to be a list of dicts, endDateTime parsed with "
          "fromisoformat() unguarded, displayName used as the subject unchecked"),
    Known("rule", "sharepoint_sharing", "tenant_policies/sharepoint_settings", "sharingCapability:as_list",
          "rules.py:sharepoint_sharing: SHARING_LEVELS.get() with an unhashable value"),
    Known("rule", "guest_invites", "tenant_policies/authorization_policy", "allowInvitesFrom:as_list",
          "rules.py:guest_invites: dict .get() with an unhashable value"),
    # rules.py + dnscheck.py: email domain checks
    *[Known("rule", key, "domain_health/domain", cases, DNS)
      for key, cases in (("spf_record", "dkim|spf_lookups|spf_records:none domain:removed"),
                         ("dmarc_policy", "dkim|spf_lookups|spf_records:none domain:removed"),
                         ("dkim_enabled", "dkim|mx|spf_lookups|spf_records:none domain:removed"))],
    *[Known("rule", key, "domain_health/domain", "domain:none|as_list", DNS_SUBJECT)
      for key in ("spf_record", "dmarc_policy", "dkim_enabled")],
    # reports/data_reports.py
    Known("report", "mailboxes", "inbox_rules/inbox_rule", "displayName|moveToFolderName:as_list", INBOX),
    Known("report", "mailboxes", "inbox_rules/inbox_rule", "mailbox:as_list",
          "data_reports.py:mailboxes: sorts inbox rules with .lower() on mailbox unchecked"),
    Known("report", "mailboxes", "mailboxes/accepted_domain", "DomainName:as_list", DOMAINS),
    Known("report", "mailboxes", "mailboxes/mailbox",
          "ForwardingRecipient:as_string|as_list ForwardingSmtpAddress:as_list", FORWARDING),
    Known("report", "mailboxes", "mailboxes/mailbox",
          "ExternalDirectoryObjectId|PrimarySmtpAddress|RecipientTypeDetails:as_list",
          "data_reports.py:mailboxes/_type: mailbox fields used as dict keys unchecked"),
    Known("report", "mailboxes", "mailboxes/mailbox_permission", "mailboxType:as_list",
          "data_reports.py:_type: dict .get() with an unhashable value"),
    Known("report", "apps", "app_consents/app_permission", "permission:as_list", APP_RISKS),
    Known("report", "apps", "app_consents/delegated_grant", "principalId:as_list", APP_RISKS),
    Known("report", "apps", "app_consents/service_principal", "displayName:as_list", APP_RISKS),
    Known("report", "apps", "app_consents/service_principal", "publisherName|verifiedPublisher:as_list",
          "data_reports.py:apps: publisher concatenated with a string unchecked"),
    Known("report", "apps", "app_consents/application",
          "credentials:as_string credentials[].endDateTime:ts_garbage displayName:as_list",
          "data_reports.py:apps/_ts: credentials assumed to be a list of dicts, endDateTime parsed with "
          "fromisoformat() unguarded, displayName sorted with .lower()"),
    Known("report", "conditional-access", "admin_roles/role_assignment",
          "principalId:removed|as_list roleDefinitionId:as_list", DIRECTORY),
    Known("report", "conditional-access", "admin_roles/role_definition", "displayName|templateId:as_list",
          f"{DIRECTORY}; coverage.py:describe joins role names unchecked"),
    Known("report", "conditional-access", "conditional_access/ca_policy", "displayName|state:as_list",
          "data_reports.py:conditional_access: sorts policies with .lower() and a dict lookup on state unchecked"),
    Known("report", "conditional-access", "users/user", "userPrincipalName:as_list",
          "coverage.py:user_coverage: sorts accounts with .lower() on the name unchecked"),
    Known("report", "domains", "domain_health/domain", "dkim|mx|spf_lookups|spf_records:none domain:removed",
          f"{DNS}; data_reports.py:domains indexes r['domain'] directly"),
    Known("report", "guests", "tenant_policies/authorization_policy", "allowInvitesFrom:as_list",
          "data_reports.py:guests: INVITES.get() with an unhashable value"),
    Known("report", "guests", "tenant_policies/sharepoint_settings", "sharingCapability:as_list",
          "data_reports.py:guests: SHARING_LEVELS.get() with an unhashable value"),
    Known("report", "guests", "users/user", f"{SIGN_IN_FIELDS}:as_string|as_list|ts_garbage", SIGN_IN),
    # changes.py
    Known("changes", "admin_changes", "admin_roles/role_assignment", "principalId|roleName:as_list roleName:removed",
          "changes.py:admin_changes: o['roleName'] indexed directly and (principalId, roleName) used as a key"),
    Known("changes", "app_changes", "app_consents/app_permission", "clientId|permission:removed|none|as_list",
          "changes.py:app_changes: joins permission names that can be null; a missing clientId becomes a null "
          "subject"),
    Known("changes", "ca_changes", "conditional_access/ca_policy", "state:as_list",
          "changes.py:ca_changes: STATE_NAMES.get() with an unhashable value"),
    Known("changes", "forwarding_changes", "mailboxes/mailbox",
          "ForwardingRecipient:as_string|as_list ForwardingSmtpAddress:as_list", FORWARDING),
]


def fuzz(s: Seeded, target: str, key: str, only: Known | None = None) -> list[str]:
    """Run one target against every degradation of the kinds it reads; return what went wrong.

    Degradations listed in ``KNOWN`` are skipped; with ``only``, they're the only ones run.
    """
    run = RUNNERS[target]
    d = s.degrader
    problems = []
    with patch.object(store, "run_objects", d):
        d.case, d.reads = None, set()
        baseline = run(s, key)
        assert not baseline, f"{target} {key} is wrong on undamaged data: {baseline}"
        # A target that never reads a kind behaves identically whatever is done to it.
        for kind in sorted(d.reads & s.cases.keys()):
            for case in s.cases[kind]:
                if only is not None:
                    if not only.matches(case):
                        continue
                elif any(k.target == target and k.key == key and k.matches(case) for k in KNOWN):
                    continue
                d.case = case
                try:
                    found = run(s, key)
                except Exception as exc:  # noqa: BLE001 - every crash is a finding
                    found = [f"{type(exc).__name__} in {_where(exc)}: {exc}"]
                finally:
                    d.case = None
                problems += [f"{case.name}: {p}" for p in found]
    return problems


# -- The fuzz suite ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", [r.key for r in RULES])
def test_rule_survives_degraded_data(seeded, key):
    problems = fuzz(seeded, "rule", key)
    assert not problems, f"rule {key}:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("key", list(data_reports.REPORTS))
def test_report_survives_degraded_data(seeded, key):
    problems = fuzz(seeded, "report", key)
    assert not problems, f"report {key}:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("name", sorted(CHANGE_FEEDS))
def test_changes_survive_degraded_data(seeded, name):
    problems = fuzz(seeded, "changes", name)
    assert not problems, f"{name}:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("known", [
    pytest.param(k, id=k.id, marks=pytest.mark.xfail(strict=True, reason=f"known: {k.cause}")) for k in KNOWN])
def test_known_crashes(seeded, known):
    problems = fuzz(seeded, known.target, known.key, only=known)
    assert not problems, "\n".join(problems)


def test_fuzz_finds_a_crash(seeded):
    """The harness itself: a check that trips over a missing field is reported."""
    def fragile(snap, now):
        return ("pass", "ok", []) if all(d["deviceName"] for d in snap.objects("devices", "managed_device")) \
            else ("pass", "", [])

    with patch.dict(RUNNERS, {"rule": lambda s, key: run_rule(s, Rule("fragile", "Fragile", "Test", "", fragile))}):
        problems = fuzz(seeded, "rule", "fragile")
    assert any("managed_device.deviceName:removed: KeyError" in p for p in problems)
    assert not any("entra_device" in p for p in problems)  # never read, so never degraded


# -- Guards: one broken check or report can't take the rest down ------------------------------


def test_evaluate_reports_a_crashing_check_as_unavailable(seeded, caplog):
    def broken(snap, now):
        raise KeyError("principalId")

    with patch("officesentry.rules.RULES", [*RULES, Rule("broken", "Broken check", "Test", "Fix it", broken)]):
        with caplog.at_level(logging.ERROR, logger="officesentry.rules"):
            results = {r.key: r for r in evaluate(TenantSnapshot(seeded.conn, seeded.tenant), seeded.now)}
    assert results["broken"].status == "unavailable"
    assert results["broken"].summary == "This check couldn't run: KeyError"
    assert results["broken"].findings == [] and results["broken"].title == "Broken check"
    assert len(results) == len(RULES) + 1 and results["device_compliance"].status == "fail"
    assert "Check broken failed" in caplog.text and "KeyError" in caplog.text


def test_evaluate_catches_findings_that_cant_be_sorted(seeded):
    def unnamed(snap, now):
        return ("fail", "1 problem", [Finding("high", None, "No name")])

    with patch("officesentry.rules.RULES", [Rule("unnamed", "Unnamed", "Test", "", unnamed)]):
        [result] = evaluate(TenantSnapshot(seeded.conn, seeded.tenant), seeded.now)
    assert (result.status, result.summary) == ("unavailable", "This check couldn't run: AttributeError")


def test_report_build_reports_a_crashing_builder_as_unavailable(seeded, caplog):
    def broken(snap, now, report):
        report.sections.append(data_reports.Section("Half built", ["A"]))
        raise ValueError("not-a-date")

    entry = ("Broken", "A report that crashes.", ("devices",), broken)
    with patch.dict(data_reports.REPORTS, {"broken": entry}):
        with caplog.at_level(logging.ERROR, logger="officesentry.reports.data_reports"):
            report = data_reports.build(seeded.conn, seeded.tenant, "broken", seeded.now)
    assert report.unavailable == "This report couldn't be built: ValueError"
    assert report.sections == [] and report.sources[0][0] == "Devices"
    assert "Report broken failed" in caplog.text
    assert data_reports.to_xlsx(report)  # the export still works


def test_guarded_crash_leaves_findings_untouched(db, connection_id):
    tid = store.create_tenant(db, "Alpha Ltd", "alpha.onmicrosoft.com", connection_id)
    seed_tenant(db, tid)
    tenant = store.get_tenant(db, tid)
    findings.sync(db, tenant)
    before = db.execute("SELECT COUNT(*) FROM findings WHERE rule_key = 'device_compliance'"
                        " AND resolved_at IS NULL").fetchone()[0]
    assert before

    def broken(snap, now):
        raise RuntimeError("bug")

    rules = [Rule(r.key, r.title, r.category, r.recommendation, broken if r.key == "device_compliance" else r.check)
             for r in RULES]
    with patch("officesentry.rules.RULES", rules):
        stats = findings.sync(db, tenant)
    assert stats["resolved"] == 0  # a crash is "couldn't run", never "fixed"
    assert db.execute("SELECT COUNT(*) FROM findings WHERE rule_key = 'device_compliance'"
                      " AND resolved_at IS NULL").fetchone()[0] == before


# -- Devices regressions (fixed root causes) ---------------------------------------------------


def test_device_records_with_wrong_types_are_cleaned():
    from officesentry import devices

    record = {"_id": "d1", "deviceName": ["laptop"], "operatingSystem": "Windows", "osVersion": 10,
              "lastSyncDateTime": ["2026-01-01T00:00:00Z"], "isEncrypted": "yes", "accountEnabled": True}
    assert devices.clean(record) == {"_id": "d1", "deviceName": None, "operatingSystem": "Windows",
                                     "osVersion": None, "lastSyncDateTime": None, "isEncrypted": None,
                                     "accountEnabled": True}


def test_device_checks_and_report_survive_lists_where_text_belongs(db, connection_id):
    tid = store.create_tenant(db, "Alpha Ltd", "alpha.onmicrosoft.com", connection_id)
    seed_tenant(db, tid)
    tenant = store.get_tenant(db, tid)
    for kind in ("entra_device", "managed_device"):
        for row in db.execute("SELECT run_id, object_id, data FROM objects WHERE kind = ?", (kind,)).fetchall():
            data = {k: [v] if isinstance(v, str) else v for k, v in json.loads(row["data"]).items()}
            db.execute("UPDATE objects SET data = ? WHERE run_id = ? AND kind = ? AND object_id = ?",
                       (json.dumps(data), row["run_id"], kind, row["object_id"]))
    with patch("officesentry.rules.logging") as log:
        results = {r.key: r for r in evaluate(TenantSnapshot(db, tenant))}
    log.getLogger.assert_not_called()  # nothing crashed
    for key in ("device_compliance", "device_encryption", "supported_windows", "intune_enrolled",
                "device_checkin", "stale_devices"):
        assert results[key].status in ("pass", "unavailable"), key  # every value unreadable: nothing to flag
    report = data_reports.build(db, tenant, "devices")
    assert report.unavailable is None and not check_report(report)


# -- End to end: one heavily degraded tenant -----------------------------------------------------

# Kinds whose degradation still crashes the monthly review or evidence pack in other threads' code
# (listed in docs/HANDOFF-cloud-session.md). The passing end-to-end test leaves just these intact.
REVIEW_CRASHES = {
    "subscribed_sku": "licences.py:licence_summary/sku_name (called by monthly_review.build) indexes skuId and "
                      "uses skuPartNumber as a dict key unchecked",
    "app_permission": "changes.py:app_changes (all_changes, called by monthly_review.build) uses permission "
                      "values as set members unchecked",
    "secure_score_daily": "monthly_review.py:_secure_score_points: date.fromisoformat(row['date']) on unchecked "
                          "values",
    "role_definition": "coverage.py:directory (evidence._extra_evidence) uses role IDs as dict keys unchecked",
    "user": "coverage.py:user_coverage (evidence._extra_evidence) sorts with .lower() on names unchecked",
}


def _wreck(row: dict, i: int) -> dict:
    """Degrade a stored object a different way for each field, varying by object."""
    out = {}
    for n, (k, v) in enumerate(sorted(row.items())):
        style = (i + n) % 5
        if style == 0:
            continue
        if style == 1:
            out[k] = None
        elif style == 2 and _is_timestamp(k, [v]):
            out[k] = "not-a-date"
        elif style == 2 and isinstance(v, list):
            out[k] = "unexpected text"
        elif style == 3 and isinstance(v, str):
            out[k] = ["unexpected", "list"]
        else:
            out[k] = v
    return out


def wreck_tenant(db, connection_id, spare=()):
    """A tenant collected twice, then every object of the latest collection degraded (but kinds in ``spare``)."""
    tid = store.create_tenant(db, "Alpha Ltd", "alpha.onmicrosoft.com", connection_id)
    seed_tenant(db, tid)
    findings.sync(db, store.get_tenant(db, tid))
    old = (datetime.now(UTC) - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute("UPDATE runs SET finished_at = ?", (old,))
    db.execute("UPDATE findings SET first_seen = ?, last_seen = ?", (old, old))
    seed_tenant(db, tid)
    latest = [r["id"] for r in db.execute("SELECT MAX(id) AS id FROM runs GROUP BY collector")]
    marks = ",".join("?" * len(latest))
    rows = db.execute(f"SELECT run_id, kind, object_id, data FROM objects WHERE run_id IN ({marks})"
                      " ORDER BY run_id, kind, object_id", latest).fetchall()
    position: dict[str, int] = {}
    for row in rows:
        i = position[row["kind"]] = position.get(row["kind"], -1) + 1  # per kind, so new kinds don't shift others
        if row["kind"] not in spare:
            db.execute("UPDATE objects SET data = ? WHERE run_id = ? AND kind = ? AND object_id = ?",
                       (json.dumps(_wreck(json.loads(row["data"]), i)), row["run_id"], row["kind"], row["object_id"]))
    return store.get_tenant(db, tid)


@pytest.fixture
def wrecked(db, connection_id):
    return wreck_tenant(db, connection_id)


def test_findings_sync_survives_a_wrecked_tenant(db, wrecked):
    results = evaluate(TenantSnapshot(db, wrecked))
    assert len(results) == len(RULES)
    assert all(r.status in ("pass", "fail", "unavailable") for r in results)
    assert all(r.findings or r.status != "fail" for r in results)
    crashed = {r.key for r in results if r.summary.startswith("This check couldn't run")}
    assert crashed  # the guard did its job ...
    assert not crashed & {r.key for r in RULES if r.category == "Devices"}  # ... and device checks don't need it
    stats = findings.sync(db, wrecked)
    assert set(stats) == {"new", "reopened", "resolved"}


@pytest.mark.parametrize("key", list(data_reports.REPORTS))
def test_report_survives_a_wrecked_tenant(db, wrecked, key):
    report = data_reports.build(db, wrecked, key)
    assert not check_report(report)
    if key == "devices":
        assert report.unavailable is None and report.sections
    assert data_reports.to_xlsx(report)


def test_monthly_review_and_evidence_survive_a_wrecked_tenant(db, connection_id, client):
    tenant = wreck_tenant(db, connection_id, spare=REVIEW_CRASHES)
    findings.sync(db, tenant)
    review = monthly_review.build(db, tenant)
    assert len(review.results) == len(RULES)
    assert all(isinstance(c, changes.Change) for c in review.changes)
    pack = evidence.build(db, tenant)
    assert len(pack.controls) == len(evidence.CONTROLS)
    assert evidence.to_xlsx(pack)

    make_user(db, "admin", all_tenants=True)
    login(client, "admin")
    pages = [f"/tenants/{tenant['id']}/{p}" for p in ("report", "report.xlsx", "evidence", "evidence.xlsx",
                                                      "findings")]
    pages += [f"/tenants/{tenant['id']}/data/{key}{ext}" for key in data_reports.REPORTS for ext in ("", ".xlsx")]
    assert {p: client.get(p).status_code for p in pages} == {p: 200 for p in pages}


@pytest.mark.xfail(strict=True, raises=(TypeError, ValueError, KeyError, AttributeError),
                   reason="known: " + "; ".join(REVIEW_CRASHES.values()))
def test_monthly_review_survives_a_fully_wrecked_tenant(db, wrecked):
    monthly_review.build(db, wrecked)
    evidence.build(db, wrecked)
