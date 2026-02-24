"""
portal/routes/jobs.py — Job status and polling routes.
"""

from flask import Blueprint, render_template, request, jsonify

from database import get_job, list_jobs, list_tenants
from helpers import login_required

bp = Blueprint("jobs", __name__)


@bp.route("/jobs")
@login_required
def jobs_page():
    tenant_id = request.args.get("tenant_id")
    all_jobs = list_jobs(tenant_id=tenant_id, limit=100)
    tenants = {t["id"]: t["name"] for t in list_tenants()}
    return render_template("jobs.html", jobs=all_jobs, tenants=tenants,
                           filter_tenant=tenant_id)


@bp.route("/jobs/<job_id>/status")
@login_required
def job_status(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    tail = job["output"][-80:]
    return jsonify({
        "status":  job["status"],
        "label":   job["label"],
        "type":    job["type"],
        "started": job["started_at"],
        "ended":   job["ended_at"],
        "output":  tail,
    })
