"""Reports listing, download, and deletion routes."""

import shutil

from flask import render_template, request, abort, send_file, jsonify

from helpers import login_required, current_user
from database import list_tenants, list_saved_views, log_action

from routes.reports import (
    bp, log, _collect_reports, _validate_report_path, TYPE_BADGE_COLORS,
)


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
