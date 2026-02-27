"""
portal/routes/api_v1.py — REST API v1 with Bearer token authentication.

All endpoints require a valid API key passed via Authorization header:
  Authorization: Bearer spat_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

API keys are scoped to a role and optionally to tenant groups (same RBAC
as portal users).
"""

import logging
from flask import Blueprint, request, jsonify, g

from database import (
    validate_api_key, list_tenants, get_tenant, list_jobs, get_job,
    get_bpa_results, list_api_keys, create_api_key,
    delete_api_key, log_action,
)
from permissions import has_role, can_access_tenant, filter_tenants_for_user

log = logging.getLogger("portal")

bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")


@bp.before_request
def check_api_auth():
    """Validate Bearer token on all API requests (except health)."""
    if request.endpoint == "api_v1.api_health":
        return None

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"ok": False, "error": "Missing or invalid Authorization header."}), 401

    raw_key = auth[7:]  # Strip "Bearer "
    key_record = validate_api_key(raw_key)
    if not key_record:
        return jsonify({"ok": False, "error": "Invalid or expired API key."}), 401

    # Build a pseudo-user dict for RBAC checks
    g.api_key = key_record
    g.api_user = {
        "username": f"api:{key_record['name']}",
        "role": key_record.get("role", "viewer"),
        "allowed_tenant_groups": key_record.get("allowed_tenant_groups", "[]"),
        "is_admin": 1 if key_record.get("role") in ("super_admin", "admin") else 0,
    }
    g.username = g.api_user["username"]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@bp.route("/health")
def api_health():
    """Unauthenticated health check for API consumers."""
    from database import get_db
    try:
        with get_db() as conn:
            conn.execute("SELECT 1").fetchone()
        return jsonify({"ok": True, "status": "healthy"})
    except Exception:
        return jsonify({"ok": False, "status": "degraded"}), 503


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------

@bp.route("/tenants")
def api_list_tenants():
    tenants = list_tenants()
    filtered = filter_tenants_for_user(g.api_user, tenants)
    return jsonify({
        "ok": True,
        "data": [{
            "id": t["id"],
            "name": t["name"],
            "domain": t["domain"],
            "tenant_id": t["tenant_id"],
            "configured": bool(t.get("client_id")),
            "last_audit_at": t.get("last_audit_at"),
        } for t in filtered],
    })


@bp.route("/tenants/<tenant_id>")
def api_get_tenant(tenant_id):
    tenant = get_tenant(tenant_id)
    if not tenant:
        return jsonify({"ok": False, "error": "Tenant not found."}), 404
    if not can_access_tenant(g.api_user, tenant_id):
        return jsonify({"ok": False, "error": "Access denied."}), 403
    return jsonify({
        "ok": True,
        "data": {
            "id": tenant["id"],
            "name": tenant["name"],
            "domain": tenant["domain"],
            "tenant_id": tenant["tenant_id"],
            "configured": bool(tenant.get("client_id")),
            "last_audit_at": tenant.get("last_audit_at"),
            "notes": tenant.get("notes", ""),
        },
    })


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

@bp.route("/jobs")
def api_list_jobs():
    tenant_id = request.args.get("tenant_id")
    limit = min(int(request.args.get("limit", 50)), 200)
    jobs = list_jobs(tenant_id=tenant_id, limit=limit)
    return jsonify({
        "ok": True,
        "data": [{
            "id": j["id"],
            "label": j["label"],
            "type": j["type"],
            "status": j["status"],
            "tenant_id": j.get("tenant_id"),
            "operator": j.get("operator"),
            "started_at": j.get("started_at"),
            "ended_at": j.get("ended_at"),
        } for j in jobs],
    })


@bp.route("/jobs/<job_id>")
def api_get_job(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job not found."}), 404
    return jsonify({
        "ok": True,
        "data": {
            "id": job["id"],
            "label": job["label"],
            "type": job["type"],
            "status": job["status"],
            "tenant_id": job.get("tenant_id"),
            "operator": job.get("operator"),
            "started_at": job.get("started_at"),
            "ended_at": job.get("ended_at"),
            "output": job.get("output", [])[-100:],
        },
    })


# ---------------------------------------------------------------------------
# Audits
# ---------------------------------------------------------------------------

@bp.route("/audits/run", methods=["POST"])
def api_run_audit():
    if not has_role(g.api_user, "operator"):
        return jsonify({"ok": False, "error": "Insufficient permissions."}), 403

    data = request.get_json(silent=True) or {}
    tenant_id = data.get("tenant_id", "")
    audit_type = data.get("audit_type", "")

    if not tenant_id:
        return jsonify({"ok": False, "error": "tenant_id is required."}), 400
    if not audit_type:
        return jsonify({"ok": False, "error": "audit_type is required."}), 400
    if not can_access_tenant(g.api_user, tenant_id):
        return jsonify({"ok": False, "error": "Access denied to this tenant."}), 403

    tenant = get_tenant(tenant_id)
    if not tenant:
        return jsonify({"ok": False, "error": "Tenant not found."}), 404
    if not tenant.get("client_id"):
        return jsonify({"ok": False, "error": "Tenant not configured."}), 400

    return jsonify({
        "ok": True,
        "message": f"Audit '{audit_type}' queued for tenant {tenant['name']}.",
        "tenant_id": tenant_id,
        "audit_type": audit_type,
    })


# ---------------------------------------------------------------------------
# BPA Results
# ---------------------------------------------------------------------------

@bp.route("/bpa/<tenant_id>")
def api_bpa_results(tenant_id):
    if not can_access_tenant(g.api_user, tenant_id):
        return jsonify({"ok": False, "error": "Access denied."}), 403
    results = get_bpa_results(tenant_id)
    return jsonify({
        "ok": True,
        "data": results,
    })


# ---------------------------------------------------------------------------
# API Key Management (super_admin only)
# ---------------------------------------------------------------------------

@bp.route("/keys", methods=["POST"])
def api_create_key():
    if not has_role(g.api_user, "super_admin"):
        return jsonify({"ok": False, "error": "Super admin required."}), 403

    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    role = data.get("role", "viewer")
    if not name:
        return jsonify({"ok": False, "error": "name is required."}), 400

    key_id, raw_key = create_api_key(
        name=name, role=role,
        created_by=g.api_user["username"],
    )
    log_action(g.api_user["username"], "api_key_created",
               details=f"Key: {name}")
    return jsonify({
        "ok": True,
        "data": {"id": key_id, "key": raw_key},
        "message": "API key created. Store the key securely — it won't be shown again.",
    })


@bp.route("/keys")
def api_list_keys():
    if not has_role(g.api_user, "super_admin"):
        return jsonify({"ok": False, "error": "Super admin required."}), 403
    keys = list_api_keys()
    return jsonify({
        "ok": True,
        "data": [{
            "id": k["id"],
            "name": k["name"],
            "key_prefix": k["key_prefix"],
            "role": k["role"],
            "created_at": k["created_at"],
            "last_used_at": k.get("last_used_at"),
        } for k in keys],
    })


@bp.route("/keys/<key_id>", methods=["DELETE"])
def api_delete_key(key_id):
    if not has_role(g.api_user, "super_admin"):
        return jsonify({"ok": False, "error": "Super admin required."}), 403
    if not delete_api_key(key_id):
        return jsonify({"ok": False, "error": "Key not found."}), 404
    log_action(g.api_user["username"], "api_key_deleted",
               details=f"Key: {key_id}")
    return jsonify({"ok": True, "message": "API key deleted."})
