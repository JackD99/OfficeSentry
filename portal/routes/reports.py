"""
portal/routes/reports.py — Report listing, downloads, and audit trail routes.
"""

import logging
from datetime import datetime
from pathlib import Path

from flask import (
    Blueprint, render_template, request, abort, send_file,
)

from database import get_tenant, list_tenants, get_audit_log
from helpers import (
    login_required, current_user,
    tenant_output_dir, TENANTS_DIR, LEGACY_OUTPUT,
)

log = logging.getLogger("portal")

bp = Blueprint("reports", __name__)


@bp.route("/reports")
@login_required
def reports():
    tenant_id = request.args.get("tenant_id")

    def classify(name: str) -> str:
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
            files.append({
                "name":        str(rel),
                "type":        classify(f.name),
                "mtime":       datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                "size":        f"{max(1, f.stat().st_size // 1024)} KB",
                "ext":         f.suffix.lower(),
                "tenant_id":   tid,
                "tenant_name": tenants_map.get(tid, ""),
                "full_path":   str(f),
            })

    files.sort(key=lambda x: x["mtime"], reverse=True)

    tenant_list = list_tenants()
    return render_template("reports.html", files=files, tenants=tenant_list,
                           filter_tenant=tenant_id)


@bp.route("/reports/download")
@login_required
def report_download():
    """Safe file download — validates path is within an allowed directory."""
    file_path = request.args.get("path", "")
    if not file_path:
        abort(404)

    resolved = Path(file_path).resolve()

    allowed_bases = [TENANTS_DIR.resolve(), LEGACY_OUTPUT.resolve()]
    if not any(str(resolved).startswith(str(base)) for base in allowed_bases):
        log.warning("Path traversal attempt blocked: %s (user=%s)",
                    file_path, current_user())
        abort(403)

    if not resolved.exists() or not resolved.is_file():
        abort(404)

    if resolved.suffix.lower() == ".html":
        return send_file(str(resolved), mimetype="text/html")
    return send_file(str(resolved), as_attachment=True)


@bp.route("/audit-log")
@login_required
def audit_trail():
    tenant_id = request.args.get("tenant_id")
    entries = get_audit_log(limit=500, tenant_id=tenant_id)
    tenants = list_tenants()
    return render_template("audit_trail.html", entries=entries,
                           tenants=tenants, filter_tenant=tenant_id)
