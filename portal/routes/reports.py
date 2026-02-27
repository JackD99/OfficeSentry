"""
portal/routes/reports.py — Report listing, interactive viewer, saved views,
scheduling, multi-format export, PDF generation, report comparison,
client dashboard, and audit trail routes.
"""

import csv
import io
import json
import logging
from datetime import datetime
from pathlib import Path

import shutil

from flask import (
    Blueprint, render_template, request, abort, send_file, jsonify, Response,
)

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
# Helpers
# ---------------------------------------------------------------------------

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
            # Sniff the dialect
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
# Reports listing
# ---------------------------------------------------------------------------

@bp.route("/reports")
@login_required
def reports():
    tenant_id = request.args.get("tenant_id")
    files, _ = _collect_reports(tenant_id)

    # Compute summary stats
    type_counts = {}
    total_size = 0
    for f in files:
        type_counts[f["type"]] = type_counts.get(f["type"], 0) + 1
        total_size += f["size_bytes"]

    tenant_list = list_tenants()
    saved = list_saved_views(current_user())

    # Check if PDF generation is available
    from report_engine import WEASYPRINT_AVAILABLE
    pdf_available = WEASYPRINT_AVAILABLE

    return render_template("reports.html",
                           files=files, tenants=tenant_list,
                           filter_tenant=tenant_id,
                           type_counts=type_counts,
                           total_size=total_size,
                           type_badge_colors=TYPE_BADGE_COLORS,
                           saved_views=saved,
                           pdf_available=pdf_available)


# ---------------------------------------------------------------------------
# PDF Report Generation
# ---------------------------------------------------------------------------

@bp.route("/reports/<tenant_id>/pdf/<report_type>")
@login_required
def download_pdf(tenant_id, report_type):
    """Generate and download a branded PDF report."""
    from report_engine import REPORT_TYPES, WEASYPRINT_AVAILABLE

    if not WEASYPRINT_AVAILABLE:
        return jsonify({
            "ok": False,
            "error": "PDF generation requires WeasyPrint. "
                     "Install system dependencies and run: pip install weasyprint"
        }), 501

    if report_type not in REPORT_TYPES:
        return jsonify({"ok": False, "error": f"Unknown report type: {report_type}"}), 400

    tenant = get_tenant(tenant_id)
    if not tenant:
        return jsonify({"ok": False, "error": "Tenant not found"}), 404

    try:
        from report_engine import generate_pdf
        branding = get_tenant_branding(tenant_id) or get_default_branding()
        pdf_bytes, filename = generate_pdf(report_type, tenant_id, branding=branding)
        log_action(current_user(), "pdf_generated",
                   tenant_id=tenant_id,
                   details=f"PDF: {report_type}")

        return Response(
            pdf_bytes,
            mimetype="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as exc:
        log.error("PDF generation failed for %s/%s: %s", tenant_id, report_type, exc)
        return jsonify({"ok": False, "error": f"PDF generation failed: {exc}"}), 500


@bp.route("/reports/pdf-types")
@login_required
def pdf_report_types():
    """Return available PDF report types."""
    from report_engine import REPORT_TYPES, WEASYPRINT_AVAILABLE
    return jsonify({
        "ok": True,
        "available": WEASYPRINT_AVAILABLE,
        "types": {k: {"title": v["title"], "description": v["description"]}
                  for k, v in REPORT_TYPES.items()},
    })


# ---------------------------------------------------------------------------
# Report Comparison
# ---------------------------------------------------------------------------

@bp.route("/reports/compare")
@login_required
def compare_reports():
    """Compare two audit reports and highlight changes."""
    file_a = request.args.get("a", "")
    file_b = request.args.get("b", "")

    # If no files selected, show selection form
    if not file_a or not file_b:
        files, _ = _collect_reports()
        return render_template("report_comparison.html",
                               files=files, data=None,
                               type_badge_colors=TYPE_BADGE_COLORS)

    # Validate both paths
    resolved_a = _validate_report_path(file_a)
    resolved_b = _validate_report_path(file_b)
    if not resolved_a or not resolved_b:
        abort(403)
    if not resolved_a.exists() or not resolved_b.exists():
        abort(404)

    # Parse both files
    data_a = _parse_report_file(resolved_a)
    data_b = _parse_report_file(resolved_b)

    # Use common headers (prefer B's headers as the newer version)
    headers = data_b.get("headers") or data_a.get("headers", [])

    # Convert rows to sets of tuples for comparison
    rows_a = data_a.get("rows", [])
    rows_b = data_b.get("rows", [])

    # Normalize row lengths to match headers
    def normalize(row):
        return tuple(row[i] if i < len(row) else "" for i in range(len(headers)))

    set_a = set(normalize(r) for r in rows_a)
    set_b = set(normalize(r) for r in rows_b)

    added = [list(r) for r in (set_b - set_a)]
    removed = [list(r) for r in (set_a - set_b)]
    unchanged = len(set_a & set_b)

    # Detect changed rows using first column as key (if present)
    changed = []
    if headers:
        key_map_a = {}
        key_map_b = {}
        for r in rows_a:
            nr = normalize(r)
            key_map_a[nr[0]] = nr
        for r in rows_b:
            nr = normalize(r)
            key_map_b[nr[0]] = nr

        for key in set(key_map_a.keys()) & set(key_map_b.keys()):
            if key_map_a[key] != key_map_b[key]:
                changed.append({"old": list(key_map_a[key]), "new": list(key_map_b[key])})

    comparison = {
        "headers": headers,
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": unchanged,
        "file_a_name": resolved_a.name,
        "file_b_name": resolved_b.name,
    }

    files, _ = _collect_reports()
    return render_template("report_comparison.html",
                           files=files, data=comparison,
                           type_badge_colors=TYPE_BADGE_COLORS)


# ---------------------------------------------------------------------------
# Client Dashboard
# ---------------------------------------------------------------------------

@bp.route("/client-dashboard")
@login_required
def client_dashboard():
    """Read-only client-facing dashboard scoped to a single tenant."""
    from database import get_user
    from permissions import is_client_user, get_client_tenant_id
    from report_engine import WEASYPRINT_AVAILABLE

    user = get_user(current_user())
    if not user:
        abort(403)

    # Determine tenant: client users get their scoped tenant,
    # other users can view any tenant's client dashboard via query param
    if is_client_user(user):
        tenant_id = get_client_tenant_id(user)
    else:
        tenant_id = request.args.get("tenant_id")

    if not tenant_id:
        abort(404)

    tenant = get_tenant(tenant_id)
    if not tenant:
        abort(404)

    # Gather data
    bpa = get_bpa_results(tenant_id)
    score = get_latest_secure_score(tenant_id)
    branding = get_tenant_branding(tenant_id) or get_default_branding()
    reports_list, _ = _collect_reports(tenant_id)

    bpa_pass = sum(1 for r in bpa if r.get("status") == "pass")
    bpa_fail = sum(1 for r in bpa if r.get("status") == "fail")

    return render_template("client_dashboard.html",
                           tenant=tenant,
                           secure_score=score,
                           bpa_results=bpa,
                           bpa_pass=bpa_pass,
                           bpa_fail=bpa_fail,
                           reports=reports_list,
                           branding=branding,
                           pdf_available=WEASYPRINT_AVAILABLE,
                           type_badge_colors=TYPE_BADGE_COLORS)


# ---------------------------------------------------------------------------
# Branding Configuration
# ---------------------------------------------------------------------------

@bp.route("/reports/branding/<tenant_id>", methods=["GET"])
@login_required
def get_branding(tenant_id):
    """Get branding configuration for a tenant."""
    branding = get_tenant_branding(tenant_id)
    defaults = get_default_branding()
    return jsonify({"ok": True, "branding": branding or defaults})


@bp.route("/reports/branding/<tenant_id>", methods=["POST"])
@login_required
def set_branding(tenant_id):
    """Set branding configuration for a tenant."""
    company_name = request.form.get("company_name", "").strip()
    accent_color = request.form.get("accent_color", "#0d6efd").strip()
    logo_path = request.form.get("logo_path", "").strip()

    upsert_tenant_branding(
        tenant_id,
        company_name=company_name,
        accent_color=accent_color,
        logo_path=logo_path,
    )
    log_action(current_user(), "branding_updated", tenant_id=tenant_id,
               details=f"Company: {company_name}")
    return jsonify({"ok": True, "message": "Branding updated."})


@bp.route("/reports/branding/default", methods=["POST"])
@login_required
def set_default_branding():
    """Set default branding in portal.json."""
    from helpers import read_portal_config, write_portal_config

    company_name = request.form.get("company_name", "").strip()
    accent_color = request.form.get("accent_color", "#0d6efd").strip()

    cfg = read_portal_config()
    cfg["branding"] = {
        "company_name": company_name or "Office Sentry",
        "accent_color": accent_color,
        "logo_path": "",
    }
    write_portal_config(cfg)
    log_action(current_user(), "default_branding_updated",
               details=f"Company: {company_name}")
    return jsonify({"ok": True, "message": "Default branding updated."})


# ---------------------------------------------------------------------------
# Interactive Report Viewer (in-browser data table)
# ---------------------------------------------------------------------------

@bp.route("/reports/view")
@login_required
def report_viewer():
    """Interactive report viewer — parses CSV and renders sortable, filterable table."""
    file_path = request.args.get("path", "")
    view_id = request.args.get("view_id", "")

    resolved = _validate_report_path(file_path)
    if not resolved:
        log.warning("Path traversal attempt blocked in viewer: %s (user=%s)",
                    file_path, current_user())
        abort(403)

    if not resolved.exists() or not resolved.is_file():
        abort(404)

    ext = resolved.suffix.lower()

    # For HTML files, redirect to direct download (opens in browser)
    if ext == ".html":
        from flask import redirect, url_for
        return redirect(url_for("reports.report_download", path=file_path))

    # Parse data files (CSV, JSON)
    data = {"headers": [], "rows": [], "total": 0}
    if ext == ".csv":
        data = _parse_csv_file(resolved)
    elif ext == ".json":
        try:
            raw = json.loads(resolved.read_text(encoding="utf-8-sig", errors="replace"))
            if isinstance(raw, list) and raw:
                if isinstance(raw[0], dict):
                    data["headers"] = list(raw[0].keys())
                    data["rows"] = [[str(item.get(h, "")) for h in data["headers"]] for item in raw[:5000]]
                    data["total"] = len(data["rows"])
                    data["truncated"] = len(raw) > 5000
            elif isinstance(raw, dict):
                data["headers"] = list(raw.keys())
                data["rows"] = [[str(v) for v in raw.values()]]
                data["total"] = 1
        except Exception as exc:
            data["error"] = str(exc)

    # Load saved view if specified
    saved_view = None
    if view_id:
        saved_view = get_saved_view(view_id)

    saved_views = list_saved_views(current_user())
    report_type = _classify(resolved.name)

    return render_template("report_viewer.html",
                           file_path=file_path,
                           file_name=resolved.name,
                           report_type=report_type,
                           type_color=TYPE_BADGE_COLORS.get(report_type, "secondary"),
                           data=data,
                           saved_view=saved_view,
                           saved_views=saved_views,
                           ext=ext)


@bp.route("/reports/data")
@login_required
def report_data_api():
    """API endpoint for loading report data (AJAX pagination/filtering)."""
    file_path = request.args.get("path", "")
    resolved = _validate_report_path(file_path)
    if not resolved or not resolved.exists():
        return jsonify({"ok": False, "error": "File not found"}), 404

    ext = resolved.suffix.lower()
    data = {"headers": [], "rows": [], "total": 0}
    if ext == ".csv":
        data = _parse_csv_file(resolved)
    elif ext == ".json":
        try:
            raw = json.loads(resolved.read_text(encoding="utf-8-sig", errors="replace"))
            if isinstance(raw, list) and raw and isinstance(raw[0], dict):
                data["headers"] = list(raw[0].keys())
                data["rows"] = [[str(item.get(h, "")) for h in data["headers"]] for item in raw[:5000]]
                data["total"] = len(data["rows"])
        except Exception as exc:
            data["error"] = str(exc)

    return jsonify({"ok": True, **data})


# ---------------------------------------------------------------------------
# Export (multi-format with column selection)
# ---------------------------------------------------------------------------

@bp.route("/reports/export", methods=["POST"])
@login_required
def report_export():
    """Export report data in selected format with selected columns."""
    file_path = request.form.get("path", "")
    export_format = request.form.get("format", "csv")
    selected_columns = request.form.getlist("columns")

    resolved = _validate_report_path(file_path)
    if not resolved or not resolved.exists():
        return jsonify({"ok": False, "error": "File not found"}), 404

    # Parse original data
    ext = resolved.suffix.lower()
    data = {"headers": [], "rows": []}
    if ext == ".csv":
        data = _parse_csv_file(resolved, max_rows=50000)
    elif ext == ".json":
        try:
            raw = json.loads(resolved.read_text(encoding="utf-8-sig", errors="replace"))
            if isinstance(raw, list) and raw and isinstance(raw[0], dict):
                data["headers"] = list(raw[0].keys())
                data["rows"] = [[str(item.get(h, "")) for h in data["headers"]] for item in raw]
        except Exception:
            pass

    headers = data["headers"]
    rows = data["rows"]

    # Filter to selected columns if specified
    if selected_columns:
        col_indices = [i for i, h in enumerate(headers) if h in selected_columns]
        headers = [headers[i] for i in col_indices]
        rows = [[row[i] if i < len(row) else "" for i in col_indices] for row in rows]

    stem = resolved.stem
    timestamp = datetime.now().strftime("%Y%m%d-%H%M")

    if export_format == "json":
        result = []
        for row in rows:
            result.append({h: (row[i] if i < len(row) else "") for i, h in enumerate(headers)})
        content = json.dumps(result, indent=2)
        return Response(
            content,
            mimetype="application/json",
            headers={"Content-Disposition": f"attachment; filename={stem}_{timestamp}.json"}
        )

    elif export_format == "html":
        html_rows = "".join(
            "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
            for row in rows
        )
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{stem}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #0d1117; color: #c9d1d9; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #30363d; padding: 8px 12px; text-align: left; font-size: 13px; }}
th {{ background: #161b22; font-weight: 600; }}
tr:hover {{ background: rgba(255,255,255,0.03); }}
h1 {{ font-size: 1.25rem; margin-bottom: 1rem; }}
.meta {{ color: #8b949e; font-size: 0.85rem; margin-bottom: 1rem; }}
</style></head><body>
<h1>{stem}</h1>
<div class="meta">Exported {timestamp} — {len(rows)} rows, {len(headers)} columns</div>
<table><thead><tr>{"".join(f"<th>{h}</th>" for h in headers)}</tr></thead>
<tbody>{html_rows}</tbody></table></body></html>"""
        return Response(
            html,
            mimetype="text/html",
            headers={"Content-Disposition": f"attachment; filename={stem}_{timestamp}.html"}
        )

    else:  # csv (default)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(headers)
        writer.writerows(rows)
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={stem}_{timestamp}.csv"}
        )


# ---------------------------------------------------------------------------
# Saved Views
# ---------------------------------------------------------------------------

@bp.route("/reports/views", methods=["POST"])
@login_required
def save_view():
    """Save a custom report view configuration."""
    name = request.form.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "View name is required."})

    report_type = request.form.get("report_type", "")
    columns = request.form.getlist("columns")
    filters_raw = request.form.get("filters", "{}")
    sort_col = request.form.get("sort_col", "")
    sort_dir = request.form.get("sort_dir", "asc")
    chart_type = request.form.get("chart_type", "")
    chart_col = request.form.get("chart_col", "")

    try:
        filters = json.loads(filters_raw) if filters_raw else {}
    except json.JSONDecodeError:
        filters = {}

    vid = create_saved_view(
        name=name, report_type=report_type, columns=columns,
        filters=filters, sort_col=sort_col, sort_dir=sort_dir,
        chart_type=chart_type, chart_col=chart_col,
        created_by=current_user()
    )
    log_action(current_user(), "view_saved", details=f"View: {name}")
    return jsonify({"ok": True, "view_id": vid, "message": f"View '{name}' saved."})


@bp.route("/reports/views/<view_id>/delete", methods=["POST"])
@login_required
def delete_view(view_id):
    """Delete a saved view."""
    if delete_saved_view(view_id):
        return jsonify({"ok": True, "message": "View deleted."})
    return jsonify({"ok": False, "error": "View not found."}), 404


# ---------------------------------------------------------------------------
# Report Scheduling
# ---------------------------------------------------------------------------

@bp.route("/reports/schedules")
@login_required
def schedules_page():
    """Report scheduling management page."""
    schedules = list_schedules()
    tenants = list_tenants()
    tenants_map = {t["id"]: t["name"] for t in tenants}
    return render_template("report_schedules.html",
                           schedules=schedules, tenants=tenants,
                           tenants_map=tenants_map)


@bp.route("/reports/schedules/create", methods=["POST"])
@login_required
def schedule_create():
    """Create a new report schedule."""
    name = request.form.get("name", "").strip()
    tenant_id = request.form.get("tenant_id", "")
    audit_type = request.form.get("audit_type", "")
    frequency = request.form.get("frequency", "weekly")
    day_of_week = int(request.form.get("day_of_week", 1))
    hour = int(request.form.get("hour", 9))

    if not name:
        return jsonify({"ok": False, "error": "Schedule name is required."})
    if not tenant_id:
        return jsonify({"ok": False, "error": "Tenant is required."})
    if not audit_type:
        return jsonify({"ok": False, "error": "Audit type is required."})
    if frequency not in ("daily", "weekly", "biweekly", "monthly"):
        return jsonify({"ok": False, "error": "Invalid frequency."})

    # Collect audit-specific parameters
    audit_params = {}
    for key in ("site_url", "user_email", "target_user", "report_format",
                "investigation", "date_range_days"):
        val = request.form.get(key, "").strip()
        if val:
            audit_params[key] = val

    sid = create_schedule(
        name=name, tenant_id=tenant_id, audit_type=audit_type,
        audit_params=audit_params, frequency=frequency,
        day_of_week=day_of_week, hour=hour,
        created_by=current_user()
    )
    log_action(current_user(), "schedule_created",
               tenant_id=tenant_id, details=f"Schedule: {name}")
    return jsonify({"ok": True, "schedule_id": sid, "message": f"Schedule '{name}' created."})


@bp.route("/reports/schedules/<schedule_id>/toggle", methods=["POST"])
@login_required
def schedule_toggle(schedule_id):
    """Enable or disable a schedule."""
    s = get_schedule(schedule_id)
    if not s:
        return jsonify({"ok": False, "error": "Schedule not found."}), 404
    new_enabled = 0 if s["enabled"] else 1
    update_schedule(schedule_id, enabled=new_enabled)
    state = "enabled" if new_enabled else "disabled"
    log_action(current_user(), f"schedule_{state}", details=f"Schedule: {s['name']}")
    return jsonify({"ok": True, "enabled": new_enabled, "message": f"Schedule {state}."})


@bp.route("/reports/schedules/<schedule_id>/delete", methods=["POST"])
@login_required
def schedule_delete(schedule_id):
    """Delete a schedule."""
    s = get_schedule(schedule_id)
    if not s:
        return jsonify({"ok": False, "error": "Schedule not found."}), 404
    delete_schedule(schedule_id)
    log_action(current_user(), "schedule_deleted", details=f"Schedule: {s['name']}")
    return jsonify({"ok": True, "message": f"Schedule '{s['name']}' deleted."})


# ---------------------------------------------------------------------------
# SMTP Configuration
# ---------------------------------------------------------------------------

@bp.route("/reports/smtp-config", methods=["GET"])
@login_required
def get_smtp_config():
    """Get SMTP configuration (passwords redacted)."""
    from helpers import read_portal_config
    cfg = read_portal_config()
    smtp = cfg.get("smtp", {})
    # Redact password
    if smtp.get("password_enc"):
        smtp["has_password"] = True
        del smtp["password_enc"]
    else:
        smtp["has_password"] = False
    return jsonify({"ok": True, "smtp": smtp})


@bp.route("/reports/smtp-config", methods=["POST"])
@login_required
def set_smtp_config():
    """Save SMTP configuration."""
    from helpers import read_portal_config, write_portal_config
    from database import encrypt_value
    from flask import current_app

    host = request.form.get("smtp_host", "").strip()
    port = request.form.get("smtp_port", "587").strip()
    username = request.form.get("smtp_username", "").strip()
    password = request.form.get("smtp_password", "").strip()
    sender = request.form.get("smtp_sender", "").strip()
    use_tls = request.form.get("smtp_use_tls", "true") == "true"

    cfg = read_portal_config()
    smtp_cfg = cfg.get("smtp", {})
    smtp_cfg["host"] = host
    smtp_cfg["port"] = int(port) if port.isdigit() else 587
    smtp_cfg["username"] = username
    smtp_cfg["sender"] = sender or username
    smtp_cfg["use_tls"] = use_tls

    # Only update password if a new one was provided
    if password:
        smtp_cfg["password_enc"] = encrypt_value(password, current_app.secret_key)

    cfg["smtp"] = smtp_cfg
    write_portal_config(cfg)
    log_action(current_user(), "smtp_configured", details=f"Host: {host}")
    return jsonify({"ok": True, "message": "SMTP configuration saved."})


@bp.route("/reports/smtp-test", methods=["POST"])
@login_required
def test_smtp():
    """Send a test email using the configured SMTP settings."""
    recipient = request.form.get("recipient", "").strip()
    if not recipient:
        return jsonify({"ok": False, "error": "Recipient email is required."})

    try:
        from helpers import read_portal_config
        from database import decrypt_value
        from flask import current_app
        import smtplib
        from email.mime.text import MIMEText

        cfg = read_portal_config()
        smtp = cfg.get("smtp", {})
        if not smtp.get("host"):
            return jsonify({"ok": False, "error": "SMTP not configured."})

        password = ""
        if smtp.get("password_enc"):
            password = decrypt_value(smtp["password_enc"], current_app.secret_key)

        msg = MIMEText("This is a test email from Office Sentry.")
        msg["Subject"] = "Office Sentry — SMTP Test"
        msg["From"] = smtp.get("sender", smtp.get("username", ""))
        msg["To"] = recipient

        server = smtplib.SMTP(smtp["host"], smtp.get("port", 587))
        if smtp.get("use_tls", True):
            server.starttls()
        if smtp.get("username") and password:
            server.login(smtp["username"], password)
        server.send_message(msg)
        server.quit()

        log_action(current_user(), "smtp_test", details=f"To: {recipient}")
        return jsonify({"ok": True, "message": f"Test email sent to {recipient}."})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"SMTP test failed: {exc}"})


# ---------------------------------------------------------------------------
# Downloads & Deletion (unchanged security)
# ---------------------------------------------------------------------------

@bp.route("/reports/download")
@login_required
def report_download():
    """Safe file download — validates path is within an allowed directory."""
    file_path = request.args.get("path", "")
    resolved = _validate_report_path(file_path)
    if not resolved:
        log.warning("Path traversal attempt blocked: %s (user=%s)",
                    file_path, current_user())
        abort(403)

    if not resolved.exists() or not resolved.is_file():
        abort(404)

    if resolved.suffix.lower() == ".html":
        return send_file(str(resolved), mimetype="text/html")
    return send_file(str(resolved), as_attachment=True)


@bp.route("/reports/delete", methods=["POST"])
@login_required
def report_delete():
    """Delete a report file or directory."""
    file_path = request.form.get("path", "")
    resolved = _validate_report_path(file_path)
    if not resolved:
        log.warning("Report delete path validation failed: %s (user=%s)",
                    file_path, current_user())
        return jsonify({"ok": False, "error": "Invalid path."}), 403

    if not resolved.exists():
        return jsonify({"ok": False, "error": "File not found."}), 404

    name = resolved.name
    try:
        if resolved.is_dir():
            shutil.rmtree(resolved)
        else:
            resolved.unlink()
    except OSError as exc:
        return jsonify({"ok": False, "error": f"Delete failed: {exc}"})

    log_action(current_user(), "report_deleted", details=f"File: {name}")
    return jsonify({"ok": True, "message": f"Deleted '{name}'."})


# ---------------------------------------------------------------------------
# Audit Trail
# ---------------------------------------------------------------------------

@bp.route("/audit-log")
@login_required
def audit_trail():
    tenant_id = request.args.get("tenant_id")
    entries = get_audit_log(limit=500, tenant_id=tenant_id)
    tenants = list_tenants()
    return render_template("audit_trail.html", entries=entries,
                           tenants=tenants, filter_tenant=tenant_id)
