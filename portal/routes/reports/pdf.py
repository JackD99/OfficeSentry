"""PDF report generation and report comparison routes."""

import json

from flask import render_template, request, abort, jsonify, Response

from helpers import login_required, current_user
from database import get_tenant, log_action, get_tenant_branding, get_default_branding

from routes.reports import (
    bp, log, _collect_reports, _validate_report_path, _parse_report_file,
    TYPE_BADGE_COLORS,
)


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
