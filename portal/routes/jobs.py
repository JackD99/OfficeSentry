"""
portal/routes/jobs.py — Job status, polling, and cancellation routes.
"""

from flask import Blueprint, render_template, request, jsonify

from database import get_job, list_jobs, list_tenants, list_running_jobs, log_action
from helpers import login_required, current_user, cancel_job

bp = Blueprint("jobs", __name__)


@bp.route("/jobs")
@login_required
def jobs_page():
    tenant_id = request.args.get("tenant_id")
    all_jobs = list_jobs(tenant_id=tenant_id, limit=100)
    tenants = {t["id"]: t["name"] for t in list_tenants()}
    return render_template("jobs.html", jobs=all_jobs, tenants=tenants,
                           filter_tenant=tenant_id)


@bp.route("/jobs/running-count")
@login_required
def running_count():
    """Lightweight endpoint for dashboard polling — avoids full page reload."""
    return jsonify({"count": len(list_running_jobs())})


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


@bp.route("/jobs/<job_id>/cancel", methods=["POST"])
@login_required
def job_cancel(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job not found."}), 404
    if job["status"] != "running":
        return jsonify({"ok": False, "error": "Job is not running."})
    if cancel_job(job_id):
        log_action(current_user(), "job_cancelled",
                   tenant_id=job.get("tenant_id"),
                   details=f"Job: {job['label']}")
        return jsonify({"ok": True, "message": "Job cancelled."})
    return jsonify({"ok": False, "error": "Could not cancel job (process not found)."})
