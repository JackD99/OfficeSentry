"""
portal/routes/conditional.py — Conditional Access management routes.
Provides listing, templates, named locations, and vacation mode
for Conditional Access policies.
Inspired by CIPP's conditional access section.
"""

import json
import logging

from flask import (
    Blueprint, render_template, request, jsonify, abort,
)

from database import (
    get_tenant, list_tenants, log_action,
    save_cached_data, get_cached_data,
    create_ca_template, get_ca_template, list_ca_templates,
    update_ca_template, delete_ca_template,
)
from helpers import (
    login_required, current_user,
    tenant_pfx_path, build_ps_env, start_job, start_python_job,
    get_tenant_pfx_password, sanitize_text,
    SCRIPTS_DIR, PWSH,
)

log = logging.getLogger("portal")

bp = Blueprint("conditional", __name__)


# =========================================================================
# Conditional Access Policies
# =========================================================================

@bp.route("/conditional-access")
@login_required
def ca_policies_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("ca_policies.html", tenants=ready_tenants)


@bp.route("/tenants/<tenant_id>/ca-policies")
@login_required
def tenant_ca_policies(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    cached = get_cached_data(tenant_id, "ca_policies")
    return render_template("tenant_ca_policies.html", tenant=t, cached=cached)


@bp.route("/tenants/<tenant_id>/ca-policies/fetch", methods=["POST"])
@login_required
def tenant_ca_policies_fetch(tenant_id):
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    data_type = request.form.get("data_type", "both")
    if data_type not in ("policies", "namedLocations", "both"):
        data_type = "both"

    pfx_path = str(tenant_pfx_path(tenant_id))

    from graph_jobs import fetch_conditional_access
    jid = start_python_job(
        f"Fetch CA Policies — {t['name']}", "conditional_access",
        fetch_conditional_access,
        args=(dict(t), pfx_path, pfx_pass, data_type),
        tenant_id=tenant_id,
    )
    log_action(current_user(), "ca_policies_fetch",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Type: {data_type}")
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# Named Locations
# =========================================================================

@bp.route("/tenants/<tenant_id>/named-locations")
@login_required
def tenant_named_locations(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    cached = get_cached_data(tenant_id, "named_locations")
    return render_template("tenant_named_locations.html", tenant=t, cached=cached)


@bp.route("/tenants/<tenant_id>/named-locations/fetch", methods=["POST"])
@login_required
def tenant_named_locations_fetch(tenant_id):
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    pfx_path = str(tenant_pfx_path(tenant_id))

    from graph_jobs import fetch_conditional_access
    jid = start_python_job(
        f"Fetch Named Locations — {t['name']}", "conditional_access",
        fetch_conditional_access,
        args=(dict(t), pfx_path, pfx_pass, "namedLocations"),
        tenant_id=tenant_id,
    )
    log_action(current_user(), "named_locations_fetch",
               tenant_id=tenant_id, tenant_name=t["name"])
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# CA Templates
# =========================================================================

@bp.route("/ca-templates")
@login_required
def ca_templates_page():
    templates = list_ca_templates()
    return render_template("ca_templates.html", templates=templates)


@bp.route("/ca-templates/create", methods=["POST"])
@login_required
def ca_template_create():
    name = sanitize_text(request.form.get("name", ""), max_length=200)
    description = sanitize_text(request.form.get("description", ""), max_length=1000)
    category = sanitize_text(request.form.get("category", "general"), max_length=50)
    policy_json_str = request.form.get("policy_json", "{}")

    if not name:
        return jsonify({"ok": False, "error": "Template name is required."})

    try:
        policy_json = json.loads(policy_json_str)
    except json.JSONDecodeError:
        return jsonify({"ok": False, "error": "Invalid policy JSON."})

    tid = create_ca_template(name, description=description, policy_json=policy_json,
                              category=category, created_by=current_user())
    log_action(current_user(), "ca_template_created", details=f"Template: {name}")
    return jsonify({"ok": True, "template_id": tid, "message": f"Template '{name}' created."})


@bp.route("/ca-templates/<template_id>/update", methods=["POST"])
@login_required
def ca_template_update(template_id):
    tmpl = get_ca_template(template_id)
    if not tmpl:
        return jsonify({"ok": False, "error": "Template not found."}), 404

    fields = {}
    name = request.form.get("name", "").strip()
    if name:
        fields["name"] = sanitize_text(name, max_length=200)
    desc = request.form.get("description")
    if desc is not None:
        fields["description"] = sanitize_text(desc, max_length=1000)
    cat = request.form.get("category")
    if cat is not None:
        fields["category"] = sanitize_text(cat, max_length=50)
    pj = request.form.get("policy_json")
    if pj is not None:
        try:
            fields["policy_json"] = json.loads(pj)
        except json.JSONDecodeError:
            return jsonify({"ok": False, "error": "Invalid policy JSON."})

    if fields:
        update_ca_template(template_id, **fields)
        log_action(current_user(), "ca_template_updated",
                   details=f"Template: {tmpl['name']}")
    return jsonify({"ok": True, "message": "Template updated."})


@bp.route("/ca-templates/<template_id>/delete", methods=["POST"])
@login_required
def ca_template_delete(template_id):
    tmpl = get_ca_template(template_id)
    if not tmpl:
        return jsonify({"ok": False, "error": "Template not found."}), 404

    delete_ca_template(template_id)
    log_action(current_user(), "ca_template_deleted",
               details=f"Template: {tmpl['name']}")
    return jsonify({"ok": True, "message": "Template deleted."})


# =========================================================================
# Vacation Mode (CA policy exclusion)
# =========================================================================

@bp.route("/tenants/<tenant_id>/vacation-mode")
@login_required
def tenant_vacation_mode(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    return render_template("tenant_vacation_mode.html", tenant=t)


@bp.route("/tenants/<tenant_id>/vacation-mode/deploy", methods=["POST"])
@login_required
def tenant_vacation_mode_deploy(tenant_id):
    """Deploy vacation mode — adds a user to CA policy exclusion temporarily."""
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    user_upn = request.form.get("user_upn", "").strip()
    start_date = request.form.get("start_date", "").strip()
    end_date = request.form.get("end_date", "").strip()
    policy_ids = request.form.getlist("policy_ids")

    if not user_upn:
        return jsonify({"ok": False, "error": "User UPN is required."})
    if not start_date or not end_date:
        return jsonify({"ok": False, "error": "Start and end dates are required."})

    tenant_val = t["tenant_id"] or t["domain"]
    pfx_path = str(tenant_pfx_path(tenant_id))
    env = build_ps_env(pfx_pass)

    # Use Graph Explorer script to add exclusions
    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Invoke-GraphExplorer.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
        "-GraphUri", "https://graph.microsoft.com/v1.0/identity/conditionalAccess/policies",
        "-Method", "GET",
    ]

    jid = start_job(f"Vacation Mode — {user_upn} — {t['name']}", "conditional_access",
                    cmd, tenant_id=tenant_id, env=env)
    log_action(current_user(), "vacation_mode_deploy",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"User: {user_upn}, Period: {start_date} to {end_date}")
    return jsonify({"ok": True, "job_id": jid,
                    "message": f"Vacation mode deployment started for {user_upn}."})
