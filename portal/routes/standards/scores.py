"""Secure Score routes."""

from flask import render_template, request, jsonify

from helpers import (
    login_required, current_user,
    tenant_pfx_path, start_python_job, get_tenant_pfx_password,
)
from database import (
    get_tenant, list_tenants, log_action,
    get_all_latest_secure_scores,
)

from routes.standards import bp


@bp.route("/secure-scores")
@login_required
def secure_scores_page():
    tenants = list_tenants()
    scores = get_all_latest_secure_scores()
    return render_template("secure_scores.html",
                           tenants=tenants, scores=scores)


@bp.route("/secure-scores/fetch", methods=["POST"])
@login_required
def secure_score_fetch():
    """Fetch Secure Score for one or more tenants."""
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

        from graph_jobs import fetch_secure_score
        label = f"Secure Score — {t['name']}"
        jid = start_python_job(
            label, "secure_score",
            fetch_secure_score,
            args=(dict(t), pfx_path, pfx_pass),
            tenant_id=tid,
        )
        job_ids.append(jid)

    log_action(current_user(), "secure_score_fetch",
               details=f"Tenants: {len(job_ids)}")

    return jsonify({
        "ok": True,
        "job_ids": job_ids,
        "message": f"Fetching Secure Score for {len(job_ids)} tenant(s).",
    })
