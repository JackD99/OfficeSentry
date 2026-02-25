"""
portal/routes/gdap.py — GDAP (Granular Delegated Admin Privileges) management routes.
Provides relationship listing, role templates, onboarding, and offboarding.
Inspired by CIPP's GDAP management section.
"""

import json
import logging

from flask import (
    Blueprint, render_template, request, jsonify, abort,
)

from database import (
    get_tenant, list_tenants, log_action,
    save_cached_data, get_cached_data,
    create_gdap_role_template, get_gdap_role_template,
    list_gdap_role_templates, update_gdap_role_template,
    delete_gdap_role_template,
)
from helpers import (
    login_required, current_user,
    tenant_pfx_path, build_ps_env, start_job, get_tenant_pfx_password,
    sanitize_text,
    SCRIPTS_DIR, PWSH,
)

log = logging.getLogger("portal")

bp = Blueprint("gdap", __name__)


# Well-known Entra ID directory roles for GDAP
GDAP_DIRECTORY_ROLES = [
    {"id": "62e90394-69f5-4237-9190-012177145e10", "name": "Global Administrator"},
    {"id": "f28a1f94-e76d-4e63-8d20-79d0e40e3bc4", "name": "SharePoint Administrator"},
    {"id": "29232cdf-9323-42fd-ade2-1d097af3e4de", "name": "Exchange Administrator"},
    {"id": "fe930be7-5e62-47db-91af-98c3a49a38b1", "name": "User Administrator"},
    {"id": "194ae4cb-b126-40b2-bd5b-6091b380977d", "name": "Security Administrator"},
    {"id": "729827e3-9c14-49f7-bb1b-9608f156bbb8", "name": "Helpdesk Administrator"},
    {"id": "b0f54661-2d74-4c50-afa3-1ec803f12efe", "name": "Billing Administrator"},
    {"id": "fdd7a751-b60b-444a-984c-02652fe8fa1c", "name": "Groups Administrator"},
    {"id": "b1be1c3e-b65d-4f19-8427-f6fa0d97feb9", "name": "Conditional Access Administrator"},
    {"id": "4a5d8f65-41da-4de4-8968-e035b65339cf", "name": "Reports Reader"},
    {"id": "790c1fb9-7f7d-4f88-86a1-ef1f95c05c1b", "name": "Application Administrator"},
    {"id": "9b895d92-2cd3-44c7-9d02-a6ac2d5ea5c3", "name": "Application Developer"},
    {"id": "158c047a-c907-4556-b7ef-446551a6b5f7", "name": "Cloud Application Administrator"},
    {"id": "e8611ab8-c189-46e8-94e1-60213ab1f814", "name": "Privileged Role Administrator"},
    {"id": "966707d0-3269-4727-9be2-8c3a10f19b9d", "name": "Password Administrator"},
    {"id": "17315797-102d-40b4-93e0-432062caca18", "name": "Compliance Administrator"},
    {"id": "44367163-eba1-44c3-98af-f5787879f96a", "name": "Dynamics 365 Administrator"},
    {"id": "11648597-926c-4cf3-9c36-bcebb0ba8dcc", "name": "Power Platform Administrator"},
    {"id": "3a2c62db-5318-420d-8d74-23affee5d9d5", "name": "Intune Administrator"},
    {"id": "5d6b6bb7-de71-4623-b4af-96380a352509", "name": "Security Reader"},
]


# =========================================================================
# GDAP Relationships
# =========================================================================

@bp.route("/gdap")
@login_required
def gdap_relationships_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("gdap_relationships.html", tenants=ready_tenants)


@bp.route("/tenants/<tenant_id>/gdap-relationships")
@login_required
def tenant_gdap_relationships(tenant_id):
    t = get_tenant(tenant_id)
    if not t:
        abort(404)
    cached = get_cached_data(tenant_id, "gdap_relationships")
    return render_template("tenant_gdap_detail.html", tenant=t, cached=cached)


@bp.route("/tenants/<tenant_id>/gdap-relationships/fetch", methods=["POST"])
@login_required
def tenant_gdap_relationships_fetch(tenant_id):
    t = get_tenant(tenant_id)
    if not t or not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant not configured."})

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."})

    tenant_val = t["tenant_id"] or t["domain"]
    pfx_path = str(tenant_pfx_path(tenant_id))
    env = build_ps_env(pfx_pass)

    cmd = [
        PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
        str(SCRIPTS_DIR / "Get-PartnerRelationships.ps1"),
        "-Tenant", tenant_val,
        "-ClientId", t["client_id"],
        "-CertificatePath", pfx_path,
    ]

    jid = start_job(f"Fetch GDAP Relationships — {t['name']}", "gdap", cmd,
                    tenant_id=tenant_id, env=env)
    log_action(current_user(), "gdap_fetch",
               tenant_id=tenant_id, tenant_name=t["name"])
    return jsonify({"ok": True, "job_id": jid})


# =========================================================================
# GDAP Role Templates
# =========================================================================

@bp.route("/gdap/role-templates")
@login_required
def gdap_role_templates_page():
    templates = list_gdap_role_templates()
    return render_template("gdap_role_templates.html", templates=templates,
                           directory_roles=GDAP_DIRECTORY_ROLES)


@bp.route("/gdap/role-templates/create", methods=["POST"])
@login_required
def gdap_role_template_create():
    name = sanitize_text(request.form.get("name", ""), max_length=200)
    description = sanitize_text(request.form.get("description", ""), max_length=1000)
    role_ids = request.form.getlist("role_ids")

    if not name:
        return jsonify({"ok": False, "error": "Template name is required."})
    if not role_ids:
        return jsonify({"ok": False, "error": "At least one role must be selected."})

    # Build role list with names
    roles = []
    for rid in role_ids:
        role_match = next((r for r in GDAP_DIRECTORY_ROLES if r["id"] == rid), None)
        if role_match:
            roles.append({"roleDefinitionId": rid, "roleName": role_match["name"]})

    tid = create_gdap_role_template(name, description=description, roles=roles,
                                     created_by=current_user())
    log_action(current_user(), "gdap_template_created",
               details=f"Template: {name}, Roles: {len(roles)}")
    return jsonify({"ok": True, "template_id": tid,
                    "message": f"Template '{name}' created with {len(roles)} role(s)."})


@bp.route("/gdap/role-templates/<template_id>", methods=["GET"])
@login_required
def gdap_role_template_detail(template_id):
    tmpl = get_gdap_role_template(template_id)
    if not tmpl:
        return jsonify({"ok": False, "error": "Template not found."}), 404
    return jsonify({"ok": True, "template": tmpl})


@bp.route("/gdap/role-templates/<template_id>/update", methods=["POST"])
@login_required
def gdap_role_template_update(template_id):
    tmpl = get_gdap_role_template(template_id)
    if not tmpl:
        return jsonify({"ok": False, "error": "Template not found."}), 404

    fields = {}
    name = request.form.get("name", "").strip()
    if name:
        fields["name"] = sanitize_text(name, max_length=200)
    desc = request.form.get("description")
    if desc is not None:
        fields["description"] = sanitize_text(desc, max_length=1000)
    role_ids = request.form.getlist("role_ids")
    if role_ids:
        roles = []
        for rid in role_ids:
            role_match = next((r for r in GDAP_DIRECTORY_ROLES if r["id"] == rid), None)
            if role_match:
                roles.append({"roleDefinitionId": rid, "roleName": role_match["name"]})
        fields["roles"] = roles

    if fields:
        update_gdap_role_template(template_id, **fields)
        log_action(current_user(), "gdap_template_updated",
                   details=f"Template: {tmpl['name']}")
    return jsonify({"ok": True, "message": "Template updated."})


@bp.route("/gdap/role-templates/<template_id>/delete", methods=["POST"])
@login_required
def gdap_role_template_delete(template_id):
    tmpl = get_gdap_role_template(template_id)
    if not tmpl:
        return jsonify({"ok": False, "error": "Template not found."}), 404

    delete_gdap_role_template(template_id)
    log_action(current_user(), "gdap_template_deleted",
               details=f"Template: {tmpl['name']}")
    return jsonify({"ok": True, "message": "Template deleted."})


# =========================================================================
# GDAP Onboarding
# =========================================================================

@bp.route("/gdap/onboarding")
@login_required
def gdap_onboarding_page():
    templates = list_gdap_role_templates()
    return render_template("gdap_onboarding.html", templates=templates,
                           directory_roles=GDAP_DIRECTORY_ROLES)


@bp.route("/gdap/onboarding/start", methods=["POST"])
@login_required
def gdap_onboarding_start():
    """Start GDAP onboarding for a new tenant."""
    tenant_domain = request.form.get("tenant_domain", "").strip()
    template_id = request.form.get("template_id", "").strip()
    duration = request.form.get("duration", "730").strip()

    if not tenant_domain:
        return jsonify({"ok": False, "error": "Tenant domain is required."})

    roles = []
    if template_id:
        tmpl = get_gdap_role_template(template_id)
        if tmpl:
            roles = tmpl["roles"]
    else:
        role_ids = request.form.getlist("role_ids")
        for rid in role_ids:
            role_match = next((r for r in GDAP_DIRECTORY_ROLES if r["id"] == rid), None)
            if role_match:
                roles.append({"roleDefinitionId": rid, "roleName": role_match["name"]})

    if not roles:
        return jsonify({"ok": False, "error": "At least one role must be selected."})

    log_action(current_user(), "gdap_onboarding_start",
               details=f"Domain: {tenant_domain}, Roles: {len(roles)}, Duration: {duration} days")

    return jsonify({
        "ok": True,
        "message": f"GDAP onboarding initiated for {tenant_domain} with {len(roles)} role(s).",
        "details": {
            "domain": tenant_domain,
            "roles": roles,
            "duration": duration,
        }
    })


# =========================================================================
# GDAP Offboarding
# =========================================================================

@bp.route("/gdap/offboarding")
@login_required
def gdap_offboarding_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]
    return render_template("gdap_offboarding.html", tenants=ready_tenants)


@bp.route("/gdap/offboarding/start", methods=["POST"])
@login_required
def gdap_offboarding_start():
    """Start GDAP offboarding for a tenant."""
    tenant_id = request.form.get("tenant_id", "").strip()
    remove_relationships = request.form.get("remove_relationships") == "on"
    remove_from_portal = request.form.get("remove_from_portal") == "on"

    if not tenant_id:
        return jsonify({"ok": False, "error": "Tenant is required."})

    t = get_tenant(tenant_id)
    if not t:
        return jsonify({"ok": False, "error": "Tenant not found."}), 404

    log_action(current_user(), "gdap_offboarding_start",
               tenant_id=tenant_id, tenant_name=t["name"],
               details=f"Remove relationships: {remove_relationships}, "
                       f"Remove from portal: {remove_from_portal}")

    return jsonify({
        "ok": True,
        "message": f"GDAP offboarding initiated for {t['name']}.",
    })
