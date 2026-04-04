"""
portal/routes/reports — Report listing, viewer, PDF generation, comparison,
scheduling, branding, SMTP, client dashboard, and audit trail.

Split into sub-modules for maintainability. All routes register on the shared
``bp`` Blueprint which is re-exported here for ``app.py``.
"""

import logging
from pathlib import Path

from flask import Blueprint

from database import (
    get_tenant, list_tenants, get_audit_log, log_action,
    create_saved_view, get_saved_view, list_saved_views, delete_saved_view,
    create_schedule, list_schedules, get_schedule, update_schedule, delete_schedule,
    get_bpa_results, get_latest_secure_score,
    get_tenant_branding, get_default_branding, upsert_tenant_branding,
)
from helpers import (
    login_required, current_user,
    tenant_output_dir, TENANTS_DIR, LEGACY_OUTPUT,
)

log = logging.getLogger("portal")

bp = Blueprint("reports", __name__)


# ---------------------------------------------------------------------------
# Shared helpers used across sub-modules
# ---------------------------------------------------------------------------

import csv
import json
from datetime import datetime


def _classify(name: str) -> str:
    n = name.lower()
    if "securityscan" in n or "365inspect" in n: return "Security Scan"
    if "userperms"    in n: return "User Permissions"
    if "groups"       in n: return "Groups"
    if "siteperm"     in n: return "Site Permissions"
    if "external"     in n: return "External Sharing"
    if "ual" in n or "auditlog" in n: return "Audit Log"
    if "inboxrule"    in n: return "Inbox Rules"
    if "signin"       in n: return "Sign-In Logs"
    if "oauth"        in n: return "OAuth Consent"
    if "msgtrace" in n or "messagetrace" in n: return "Message Trace"
    return "Other"


TYPE_BADGE_COLORS = {
    "Security Scan": "danger",
    "User Permissions": "success",
    "Groups": "primary",
    "Site Permissions": "warning",
    "External Sharing": "info",
    "Audit Log": "secondary",
    "Inbox Rules": "danger",
    "Sign-In Logs": "warning",
    "OAuth Consent": "info",
    "Message Trace": "secondary",
    "Other": "dark",
}


def _validate_report_path(file_path: str) -> Path | None:
    """Validate that a file path is within allowed directories."""
    if not file_path:
        return None
    resolved = Path(file_path).resolve()
    allowed_bases = [TENANTS_DIR.resolve(), LEGACY_OUTPUT.resolve()]
    if not any(str(resolved).startswith(str(base)) for base in allowed_bases):
        return None
    return resolved


def _collect_reports(tenant_id: str = None) -> tuple[list, dict]:
    """Collect all report files, optionally filtered by tenant. Returns (files, tenants_map)."""
    files = []
    tenants_map = {}

    if tenant_id:
        t = get_tenant(tenant_id)
        dirs = [(tenant_id, tenant_output_dir(tenant_id))]
        if t:
            tenants_map[tenant_id] = t["name"]
    else:
        dirs = []
        for t in list_tenants():
            td = tenant_output_dir(t["id"])
            if td.exists():
                dirs.append((t["id"], td))
                tenants_map[t["id"]] = t["name"]
        if LEGACY_OUTPUT.exists():
            dirs.append(("_legacy", LEGACY_OUTPUT))
            tenants_map["_legacy"] = "(Legacy)"

    for tid, out_dir in dirs:
        if not out_dir.exists():
            continue
        for f in sorted(out_dir.rglob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
            if not f.is_file():
                continue
            rel = f.relative_to(out_dir)
            size_bytes = f.stat().st_size
            files.append({
                "name":        str(rel),
                "type":        _classify(f.name),
                "mtime":       datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                "size":        f"{max(1, size_bytes // 1024)} KB",
                "size_bytes":  size_bytes,
                "ext":         f.suffix.lower(),
                "tenant_id":   tid,
                "tenant_name": tenants_map.get(tid, ""),
                "full_path":   str(f),
            })

    files.sort(key=lambda x: x["mtime"], reverse=True)
    return files, tenants_map


def _parse_csv_file(file_path: Path, max_rows: int = 5000) -> dict:
    """Parse a CSV file and return headers + rows for the viewer."""
    try:
        with open(file_path, "r", encoding="utf-8-sig", errors="replace") as f:
            sample = f.read(4096)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample)
            except csv.Error:
                dialect = csv.excel
            reader = csv.reader(f, dialect)
            rows = []
            headers = []
            for i, row in enumerate(reader):
                if i == 0:
                    headers = [h.strip() for h in row]
                    continue
                if i > max_rows:
                    break
                rows.append(row)
        return {"headers": headers, "rows": rows, "total": len(rows), "truncated": len(rows) >= max_rows}
    except Exception as exc:
        return {"headers": [], "rows": [], "total": 0, "error": str(exc)}


def _parse_report_file(file_path: Path, max_rows: int = 50000) -> dict:
    """Parse a CSV or JSON report file into headers + rows."""
    ext = file_path.suffix.lower()
    if ext == ".csv":
        return _parse_csv_file(file_path, max_rows=max_rows)
    elif ext == ".json":
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8-sig", errors="replace"))
            if isinstance(raw, list) and raw and isinstance(raw[0], dict):
                headers = list(raw[0].keys())
                rows = [[str(item.get(h, "")) for h in headers] for item in raw[:max_rows]]
                return {"headers": headers, "rows": rows, "total": len(rows)}
        except Exception as exc:
            return {"headers": [], "rows": [], "total": 0, "error": str(exc)}
    return {"headers": [], "rows": [], "total": 0}


# ---------------------------------------------------------------------------
# Import sub-modules to register their routes on ``bp``
# ---------------------------------------------------------------------------
from routes.reports import listing, viewer, pdf, config, client  # noqa: E402, F401
