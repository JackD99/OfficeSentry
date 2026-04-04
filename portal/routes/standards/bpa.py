"""Best Practice Analyzer (BPA) routes."""

import json

from flask import render_template, request, jsonify

from helpers import (
    login_required, current_user,
    tenant_pfx_path, start_python_job, get_tenant_pfx_password,
)
from database import (
    get_tenant, list_tenants, log_action,
    save_bpa_result, get_bpa_results, get_bpa_summary, clear_bpa_results,
    get_latest_secure_score, get_all_latest_secure_scores,
    list_tenant_groups, touch_tenant_audit,
)

from routes.standards import bp
from routes.standards.definitions import BPA_CHECKS, IMPACT_COLORS
from database import list_standards_templates


@bp.route("/bpa")
@login_required
def bpa_page():
    tenants = list_tenants()
    summary = get_bpa_summary()
    all_results = get_bpa_results()
    scores = get_all_latest_secure_scores()
    groups = list_tenant_groups()
    templates = list_standards_templates()

    # Build a matrix of tenant -> check results
    tenant_results = {}
    for r in all_results:
        tid = r["tenant_id"]
        if tid not in tenant_results:
            tenant_results[tid] = {}
        tenant_results[tid][r["check_key"]] = r

    # Score map for quick lookup
    score_map = {s["tenant_id"]: s for s in scores}

    return render_template("bpa.html",
                           tenants=tenants,
                           summary=summary,
                           bpa_checks=BPA_CHECKS,
                           tenant_results=tenant_results,
                           score_map=score_map,
                           impact_colors=IMPACT_COLORS,
                           groups=groups,
                           templates=templates)


@bp.route("/bpa/scan", methods=["POST"])
@login_required
def bpa_scan():
    """Run BPA against one or more tenants."""
    tenant_ids = request.form.getlist("tenant_ids")
    if not tenant_ids:
        return jsonify({"ok": False, "error": "Select at least one tenant."})

    job_ids = []
    for tid in tenant_ids:
        t = get_tenant(tid)
        if not t or not t["client_id"]:
            continue

        pfx_pass = get_tenant_pfx_password(t)
        if not pfx_pass:
            continue

        pfx_path = str(tenant_pfx_path(tid))

        from graph_jobs import fetch_tenant_bpa
        label = f"BPA Scan — {t['name']}"
        jid = start_python_job(
            label, "bpa",
            fetch_tenant_bpa,
            args=(dict(t), pfx_path, pfx_pass),
            tenant_id=tid,
        )
        touch_tenant_audit(tid)
        job_ids.append(jid)

    log_action(current_user(), "bpa_scan",
               details=f"Tenants: {len(job_ids)}")

    return jsonify({
        "ok": True,
        "job_ids": job_ids,
        "message": f"Started BPA scan for {len(job_ids)} tenant(s).",
    })


@bp.route("/bpa/results/<tenant_id>")
@login_required
def bpa_tenant_results(tenant_id):
    """Get BPA results for a specific tenant."""
    t = get_tenant(tenant_id)
    if not t:
        return jsonify({"ok": False, "error": "Tenant not found."}), 404

    results = get_bpa_results(tenant_id=tenant_id)
    score = get_latest_secure_score(tenant_id)

    return jsonify({
        "ok": True,
        "tenant": {"id": t["id"], "name": t["name"], "domain": t["domain"]},
        "results": results,
        "secure_score": score,
        "checks": BPA_CHECKS,
    })


@bp.route("/bpa/import-results", methods=["POST"])
@login_required
def bpa_import_results():
    """Import BPA results from a completed job (called after PS script finishes)."""
    tenant_id = request.form.get("tenant_id", "")
    results_json = request.form.get("results", "")

    if not tenant_id or not results_json:
        return jsonify({"ok": False, "error": "tenant_id and results are required."})

    try:
        data = json.loads(results_json)
    except json.JSONDecodeError:
        return jsonify({"ok": False, "error": "Invalid JSON."})

    if not data.get("ok"):
        return jsonify({"ok": False, "error": data.get("error", "BPA scan failed.")})

    clear_bpa_results(tenant_id)
    count = 0
    for check in data.get("checks", []):
        save_bpa_result(
            tenant_id=tenant_id,
            check_key=check.get("check_key", ""),
            check_name=check.get("check_name", ""),
            status=check.get("status", "unknown"),
            category=check.get("category", ""),
            detail=check.get("detail", ""),
            expected=check.get("expected", ""),
            actual=check.get("actual", ""),
            impact=check.get("impact", "Medium"),
        )
        count += 1

    return jsonify({"ok": True, "message": f"Imported {count} BPA results."})
