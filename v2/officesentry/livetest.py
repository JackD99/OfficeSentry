"""Run every collector against a real test tenant using a client secret.

For development only: the product signs in with a certificate. Reads
OFFICESENTRY_TEST_TENANT_ID, OFFICESENTRY_TEST_CLIENT_ID and
OFFICESENTRY_TEST_CLIENT_SECRET (and optionally OFFICESENTRY_TEST_CLOUD: commercial,
gcc_high, dod or china, for a test tenant in a national cloud), stores the results in the configured
database as tenant "Live test" (so the portal shows them), and prints what
worked, what failed and the check results. Nothing is written to the tenant.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import clouds, dnscheck, recording, store
from .collectors import REGISTRY
from .config import Config
from .db import connect, migrate, utcnow
from .exchange import ExchangeClient, ExchangeError
from .graph import GraphClient, GraphError
from .msauth import TokenError, secret_token_provider
from .rules import evaluate
from .snapshot import TenantSnapshot
from .text import plural
from .worker import execute_run

ENV = ("OFFICESENTRY_TEST_TENANT_ID", "OFFICESENTRY_TEST_CLIENT_ID", "OFFICESENTRY_TEST_CLIENT_SECRET")


def _wrap(provider, error_cls):
    def token() -> str:
        try:
            return provider()
        except TokenError as exc:
            raise error_cls(401, exc.code, exc.message) from exc
    return token


def run(cfg: Config, only: list[str] | None = None, out_dir: Path | None = None,
        record: Path | None = None, connection_id: int | None = None) -> int:
    missing = [k for k in ENV if not os.environ.get(k)]
    if missing:
        print("Missing environment variables: " + ", ".join(missing))
        return 2
    tenant_ref, client_id, secret = (os.environ[k] for k in ENV)
    cloud_key = os.environ.get("OFFICESENTRY_TEST_CLOUD", "").strip() or clouds.DEFAULT
    if not clouds.valid(cloud_key):
        print(f"OFFICESENTRY_TEST_CLOUD must be one of {', '.join(clouds.CLOUDS)}")
        return 2
    cloud = clouds.get(cloud_key)
    if record and not cloud.is_default:
        print("The recorded fixture replays commercial endpoints; record from a commercial test tenant.")
        return 2
    conn = connect(cfg.db_path)
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
    conn.execute("INSERT INTO settings (key, value) VALUES ('live_test_client_id', ?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (client_id,))

    log: list = []
    session = recording.RecordingSession(log) if record else None
    graph = GraphClient(_wrap(secret_token_provider(tenant_ref, client_id, secret, cloud.graph, cloud.key),
                              GraphError), session=session, cloud=cloud.key)
    exo = ExchangeClient(tenant_ref, _wrap(secret_token_provider(tenant_ref, client_id, secret, cloud.exchange,
                                                                 cloud.key), ExchangeError),
                         session=session, cloud=cloud.key)
    real_lookup = dnscheck.lookup
    if record:
        dnscheck.lookup = recording.recording_lookup(log, real_lookup)
    keys = [k for k in REGISTRY if not only or k in only]
    failures = 0
    for key in keys:
        store.enqueue_run(conn, tenant_id, key, trigger="manual")
        run_row = store.claim_next_run(conn)
        # The connection check looks up the app that actually signs in: the test app, not the tenant's connection.
        status = execute_run(conn, run_row, lambda t, c: graph, lambda t, c: exo, client_id=client_id)
        finished = conn.execute("SELECT * FROM runs WHERE id = ?", (run_row["id"],)).fetchone()
        count = finished["object_count"]
        print(f"{REGISTRY[key].title:32} {status:10} {count:6} objects  {finished['error'] or ''}")
        failures += status == "failed"
    dnscheck.lookup = real_lookup
    if failures < len(keys):
        # Data came back, so admin consent exists; without this the portal says "Awaiting consent".
        conn.execute("UPDATE tenants SET consented_at = COALESCE(consented_at, ?) WHERE id = ?",
                     (utcnow(), tenant_id))

    if record:
        fixture = recording.scrub(log, tenant_ref, client_id)
        record.write_text(json.dumps(fixture, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Recorded {plural(len(fixture['entries']), 'scrubbed response')} to {record}")

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
    print(f"\nTenant id {tenant_id} in {cfg.db_path}; {plural(failures, 'collector')} failed.")
    return 1 if failures else 0
