"""Run every collector against a real test tenant using a client secret.

For development only: the product signs in with a certificate. Reads
OFFICESENTRY_TEST_TENANT_ID, OFFICESENTRY_TEST_CLIENT_ID and
OFFICESENTRY_TEST_CLIENT_SECRET (and optionally OFFICESENTRY_TEST_CLOUD: commercial,
gcc_high, dod or china, for a test tenant in a national cloud), stores the results in the configured
database as tenant "Live test" (so the portal shows them), and prints what
worked, what failed and the check results. Nothing is written to the tenant.

In GitHub Actions with ``id-token: write`` and no secret, it signs in with the
workflow's own OIDC token instead, which the test app trusts through a federated
credential (``.github/workflows/canary.yml``). ``canary`` compares the responses'
shape with the recorded fixture (``canary.py``) and writes a report; it prints no
check results or errors, since those can name the tenant's people and CI logs
may be public.

Where plain DNS is blocked (some sandboxes and proxies answer every query with
nothing), ``dns_over_https`` looks records up over HTTPS instead, so the domain
checks and a recording get real answers.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import closing
from pathlib import Path

import requests

from . import clouds, dnscheck, recording, store
from .canary import compare
from .checks import evaluate
from .collectors import REGISTRY
from .config import Config
from .db import connect, migrate, utcnow
from .exchange import ExchangeClient, ExchangeError
from .graph import GraphClient, GraphError
from .msauth import TokenError, federated_token_provider, secret_token_provider
from .snapshot import TenantSnapshot
from .text import plural
from .worker import execute_run

ENV = ("OFFICESENTRY_TEST_TENANT_ID", "OFFICESENTRY_TEST_CLIENT_ID", "OFFICESENTRY_TEST_CLIENT_SECRET")
GITHUB_OIDC = ("ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN")
# The audience Entra expects on a federated credential's token.
OIDC_AUDIENCE = "api://AzureADTokenExchange"
FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "live_tenant.json"
DOH_URL = "https://cloudflare-dns.com/dns-query"
DOH_TYPES = {"TXT": 16, "MX": 15, "CNAME": 5}
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')


def doh_lookup(name: str, rdtype: str, session: requests.Session | None = None) -> list[str]:
    """``dnscheck.lookup`` over DNS-over-HTTPS (JSON), with the same answers and errors."""
    try:
        resp = (session or requests).get(
            DOH_URL, params={"name": name, "type": rdtype}, headers={"Accept": "application/dns-json"}, timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise dnscheck.DnsError(f"{name} {rdtype}: {exc}") from exc
    if data.get("Status") == 3:  # NXDOMAIN
        return []
    if data.get("Status") != 0:
        raise dnscheck.DnsError(f"{name} {rdtype}: DNS status {data.get('Status')}")
    answers = [a for a in data.get("Answer") or [] if a.get("type") == DOH_TYPES[rdtype]]
    if rdtype == "TXT":
        return ["".join(re.sub(r"\\(.)", r"\1", part) for part in _QUOTED.findall(a["data"])) for a in answers]
    if rdtype == "MX":
        ranked = sorted((int(a["data"].split()[0]), a["data"].split()[1]) for a in answers)
        return [host.rstrip(".").lower() for _, host in ranked]
    return [a["data"].rstrip(".").lower() for a in answers]


def github_oidc_token() -> str:
    """A fresh OIDC token from GitHub Actions for Entra (needs ``permissions: id-token: write``)."""
    url, bearer = (os.environ[k] for k in GITHUB_OIDC)
    try:
        resp = requests.get(
            url, params={"audience": OIDC_AUDIENCE}, headers={"Authorization": f"bearer {bearer}"}, timeout=30
        )
        resp.raise_for_status()
        return resp.json()["value"]
    except (requests.RequestException, ValueError, KeyError) as exc:
        raise TokenError("github_oidc", f"GitHub didn't issue an OIDC token: {exc}") from exc


def _token_provider(tenant_ref: str, client_id: str, secret: str, resource: str, cloud: str):
    if secret:
        return secret_token_provider(tenant_ref, client_id, secret, resource, cloud)
    return federated_token_provider(tenant_ref, client_id, github_oidc_token, resource, cloud)


def _wrap(provider, error_cls):
    def token() -> str:
        try:
            return provider()
        except TokenError as exc:
            raise error_cls(401, exc.code, exc.message) from exc

    return token


def run(
    cfg: Config,
    only: list[str] | None = None,
    out_dir: Path | None = None,
    record: Path | None = None,
    connection_id: int | None = None,
    dns_over_https: bool = False,
    canary: Path | None = None,
) -> int:
    federated = not os.environ.get(ENV[2]) and all(os.environ.get(k) for k in GITHUB_OIDC)
    missing = [k for k in ENV if not os.environ.get(k) and not (federated and k == ENV[2])]
    if missing:
        print("Missing environment variables: " + ", ".join(missing))
        return 2
    tenant_ref, client_id, secret = (os.environ.get(k, "") for k in ENV)
    cloud_key = os.environ.get("OFFICESENTRY_TEST_CLOUD", "").strip() or clouds.DEFAULT
    if not clouds.valid(cloud_key):
        print(f"OFFICESENTRY_TEST_CLOUD must be one of {', '.join(clouds.CLOUDS)}")
        return 2
    cloud = clouds.get(cloud_key)
    if (record or canary) and not cloud.is_default:
        print("The recorded fixture replays commercial endpoints; record from a commercial test tenant.")
        return 2
    with closing(connect(cfg.db_path)) as conn:
        migrate(conn)
        connections = store.list_connections(conn)
        if connection_id is not None and connection_id not in {c["id"] for c in connections}:
            print(f"No app connection with id {connection_id}")
            return 2
        if connection_id is None and len(connections) == 1:
            connection_id = connections[0]["id"]  # so the portal can keep refreshing it with the certificate
        row = conn.execute("SELECT id, connection_id FROM tenants WHERE tenant_ref = ?", (tenant_ref,)).fetchone()
        if connection_id is not None and clouds.of(store.get_connection(conn, connection_id)).key != cloud.key:
            connection_id = None  # an app connection from another cloud couldn't refresh this tenant
        tenant_id = row["id"] if row else store.create_tenant(conn, "Live test", tenant_ref, connection_id, cloud.key)
        if row and connection_id is not None and row["connection_id"] is None:
            conn.execute("UPDATE tenants SET connection_id = ? WHERE id = ?", (connection_id, tenant_id))
        # The test app is Office Sentry's own, so checks shouldn't flag it. Its client ID isn't secret.
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('live_test_client_id', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (client_id,),
        )

        log: list = []
        session = recording.RecordingSession(log) if record or canary else None
        graph = GraphClient(
            _wrap(_token_provider(tenant_ref, client_id, secret, cloud.graph, cloud.key), GraphError),
            session=session,
            cloud=cloud.key,
        )
        exo = ExchangeClient(
            tenant_ref,
            _wrap(_token_provider(tenant_ref, client_id, secret, cloud.exchange, cloud.key), ExchangeError),
            session=session,
            cloud=cloud.key,
        )
        real_lookup = dnscheck.lookup
        answers: list = []
        dnscheck.lookup = recording.recording_lookup(answers, doh_lookup if dns_over_https else real_lookup)
        if record or canary:
            dnscheck.lookup = recording.recording_lookup(log, dnscheck.lookup)
        keys = [k for k in REGISTRY if not only or k in only]
        # Count the test app as consented while collecting: a first success on an unconsented tenant queues every
        # other collector (worker.execute_run), which would run collectors that weren't asked for, in another order.
        consented = store.get_tenant(conn, tenant_id)["consented_at"]
        conn.execute("UPDATE tenants SET consented_at = COALESCE(consented_at, ?) WHERE id = ?", (utcnow(), tenant_id))
        failures = []
        for key in keys:
            store.enqueue_run(conn, tenant_id, key, trigger="manual")
            run_row = store.claim_next_run(conn)
            # The connection check looks up the app that actually signs in: the test app, not the tenant's connection.
            status = execute_run(conn, run_row, lambda t, c: graph, lambda t, c: exo, client_id=client_id)
            finished = conn.execute("SELECT * FROM runs WHERE id = ?", (run_row["id"],)).fetchone()
            count = finished["object_count"]
            error = "" if canary else finished["error"] or ""
            print(f"{REGISTRY[key].title:32} {status:10} {count:6} objects  {error}")
            if status == "failed":
                failures.append(key)
        dnscheck.lookup = real_lookup
        if answers and not any(a["answer"] for a in answers):
            print(
                "\nEvery DNS lookup came back empty, so the domain checks are wrong. If this machine's DNS is "
                "blocked, run again with --dns-over-https."
            )
        if len(failures) == len(keys) and consented is None:
            # Nothing came back, so there may be no admin consent yet; the portal says "Awaiting consent".
            conn.execute("UPDATE tenants SET consented_at = NULL WHERE id = ?", (tenant_id,))

        if record:
            fixture = recording.scrub(log, tenant_ref, client_id)
            record.write_text(json.dumps(fixture, indent=1, sort_keys=True) + "\n", encoding="utf-8")
            print(f"Recorded {plural(len(fixture['entries']), 'scrubbed response')} to {record}")

        if canary:
            try:
                current = recording.scrub(log, tenant_ref, client_id)
            except ValueError as exc:  # names no values, only how many and their first letters
                canary.write_text(f"**The canary couldn't check the responses.** {exc}\n", encoding="utf-8")
                print(exc)
                return 1
            found = compare(
                json.loads(FIXTURE.read_text(encoding="utf-8")),
                current,
                [f"{REGISTRY[k].title} (`{k}`)" for k in failures],
            )
            canary.write_text(found.markdown(), encoding="utf-8")
            print(f"\nCanary: {'something changed' if found.failed else 'nothing changed'}; report in {canary}")
            return 1 if found.failed else 0

        tenant = store.get_tenant(conn, tenant_id)
        print("\nChecks:")
        for r in evaluate(TenantSnapshot(conn, tenant)):
            print(f"  {r.status:12} {r.title}: {r.summary}")

        if out_dir:
            from .reports import data_reports, export
            from .reports.monthly_review import build

            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "monthly-review.xlsx").write_bytes(export.to_xlsx(build(conn, tenant)))
            for key in data_reports.REPORTS:
                (out_dir / f"{key}.xlsx").write_bytes(data_reports.to_xlsx(data_reports.build(conn, tenant, key)))
            print(f"\nWrote XLSX exports to {out_dir}")
        print(f"\nTenant id {tenant_id} in {cfg.db_path}; {plural(len(failures), 'collector')} failed.")
        return 1 if failures else 0
