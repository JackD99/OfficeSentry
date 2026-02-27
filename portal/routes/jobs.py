"""
portal/routes/jobs.py — Job status, polling, cancellation, and SSE streaming routes.
"""

import json
import os
import time
from datetime import datetime

from flask import Blueprint, render_template, request, jsonify, current_app, Response

from database import get_job, list_jobs, list_tenants, list_running_jobs, log_action
from helpers import login_required, current_user, cancel_job


def _format_duration(started_at, ended_at=None):
    """Return human-readable duration string from ISO timestamps."""
    if not started_at:
        return ""
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(ended_at) if ended_at else datetime.now()
        delta = end - start
        total_secs = int(delta.total_seconds())
        if total_secs < 0:
            return ""
        if total_secs < 60:
            return f"{total_secs}s"
        mins, secs = divmod(total_secs, 60)
        if mins < 60:
            return f"{mins}m {secs}s"
        hours, mins = divmod(mins, 60)
        return f"{hours}h {mins}m"
    except (ValueError, TypeError):
        return ""

bp = Blueprint("jobs", __name__)


@bp.route("/jobs")
@login_required
def jobs_page():
    tenant_id = request.args.get("tenant_id")
    all_jobs = list_jobs(tenant_id=tenant_id, limit=100)
    # Add duration to each job for template rendering
    for job in all_jobs:
        job["duration"] = _format_duration(job.get("started_at"), job.get("ended_at"))
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
        "status":   job["status"],
        "label":    job["label"],
        "type":     job["type"],
        "started":  job["started_at"],
        "ended":    job["ended_at"],
        "duration": _format_duration(job["started_at"], job["ended_at"]),
        "output":   tail,
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


@bp.route("/jobs/<job_id>/stream")
@login_required
def job_stream(job_id):
    """Server-Sent Events stream for real-time job output.

    Uses Redis pub/sub when available, falls back to DB polling.
    """
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    def generate_redis():
        """Stream via Redis pub/sub."""
        import redis
        r = redis.Redis.from_url(
            os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
        )
        pubsub = r.pubsub()
        pubsub.subscribe(f"job:{job_id}")
        try:
            for message in pubsub.listen():
                if message["type"] == "message":
                    data = message["data"]
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")
                    yield f"data: {data}\n\n"
                    # Check if job completed
                    try:
                        parsed = json.loads(data)
                        if parsed.get("status") in ("done", "failed", "cancelled"):
                            yield f"data: {json.dumps({'status': parsed['status'], 'done': True})}\n\n"
                            return
                    except (json.JSONDecodeError, KeyError):
                        pass
        finally:
            pubsub.unsubscribe()
            pubsub.close()

    def generate_polling():
        """Fallback: poll the database every 2 seconds."""
        last_count = 0
        while True:
            current_job = get_job(job_id)
            if not current_job:
                return
            output = current_job.get("output", [])
            if len(output) > last_count:
                for line in output[last_count:]:
                    yield f"data: {json.dumps({'line': line, 'status': current_job['status']})}\n\n"
                last_count = len(output)
            if current_job["status"] != "running":
                yield f"data: {json.dumps({'status': current_job['status'], 'done': True})}\n\n"
                return
            time.sleep(2)

    # Try Redis first, fall back to polling
    try:
        import redis
        r = redis.Redis.from_url(
            os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
        )
        r.ping()
        generator = generate_redis()
    except Exception:
        generator = generate_polling()

    return Response(
        generator,
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@bp.route("/events/dashboard")
@login_required
def dashboard_events():
    """SSE stream for dashboard updates (running job count, stats)."""
    def generate():
        last_count = -1
        while True:
            running = list_running_jobs()
            count = len(running)
            if count != last_count:
                payload = json.dumps({"running_count": count})
                yield f"data: {payload}\n\n"
                last_count = count
            if count == 0 and last_count == 0:
                # Send keepalive every 15s when idle
                yield ": keepalive\n\n"
            time.sleep(3)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
