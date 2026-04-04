"""Interactive report viewer, data API, multi-format export, and saved views."""

import csv
import io
import json
from datetime import datetime

from flask import render_template, request, abort, jsonify, Response

from helpers import login_required, current_user
from database import (
    create_saved_view, get_saved_view, list_saved_views, delete_saved_view,
    log_action,
)

from routes.reports import (
    bp, log, _classify, _validate_report_path, _parse_csv_file,
    TYPE_BADGE_COLORS,
)


# ---------------------------------------------------------------------------
# Interactive Report Viewer
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
