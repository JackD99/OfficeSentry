"""Standards template CRUD and apply routes."""

from flask import render_template, request, jsonify

from helpers import login_required, current_user, sanitize_text
from database import (
    get_tenant, list_tenants, log_action,
    create_standards_template, get_standards_template, list_standards_templates,
    update_standards_template, delete_standards_template,
    assign_standard_to_tenant,
)

from routes.standards import bp
from routes.standards.definitions import BPA_CHECKS, IMPACT_COLORS


@bp.route("/standards")
@login_required
def standards_page():
    templates = list_standards_templates()
    tenants = list_tenants()
    return render_template("standards.html",
                           templates=templates, tenants=tenants,
                           bpa_checks=BPA_CHECKS,
                           impact_colors=IMPACT_COLORS)


@bp.route("/standards/templates/new", methods=["POST"])
@login_required
def template_create():
    name = sanitize_text(request.form.get("name", ""), max_length=200)
    description = sanitize_text(request.form.get("description", ""), max_length=1000)
    check_keys = request.form.getlist("checks")

    if not name:
        return jsonify({"ok": False, "error": "Template name is required."})

    valid_keys = {c["key"] for c in BPA_CHECKS}
    checks = [k for k in check_keys if k in valid_keys]

    if not checks:
        return jsonify({"ok": False, "error": "Select at least one check."})

    tid = create_standards_template(
        name, description=description, checks=checks,
        created_by=current_user()
    )
    log_action(current_user(), "template_created",
               details=f"Template: {name}, Checks: {len(checks)}")

    return jsonify({"ok": True, "template_id": tid,
                    "message": f"Template '{name}' created with {len(checks)} check(s)."})


@bp.route("/standards/templates/<template_id>", methods=["GET"])
@login_required
def template_detail(template_id):
    template = get_standards_template(template_id)
    if not template:
        return jsonify({"ok": False, "error": "Template not found."}), 404

    check_details = []
    for check in BPA_CHECKS:
        if check["key"] in template["checks"]:
            check_details.append(check)

    return jsonify({"ok": True, "template": template, "check_details": check_details})


@bp.route("/standards/templates/<template_id>/update", methods=["POST"])
@login_required
def template_update(template_id):
    template = get_standards_template(template_id)
    if not template:
        return jsonify({"ok": False, "error": "Template not found."}), 404

    name = sanitize_text(request.form.get("name", ""), max_length=200)
    description = sanitize_text(request.form.get("description", ""), max_length=1000)
    check_keys = request.form.getlist("checks")

    valid_keys = {c["key"] for c in BPA_CHECKS}
    checks = [k for k in check_keys if k in valid_keys]

    fields = {}
    if name:
        fields["name"] = name
    if description is not None:
        fields["description"] = description
    if checks:
        fields["checks"] = checks

    if fields:
        update_standards_template(template_id, **fields)
        log_action(current_user(), "template_updated",
                   details=f"Template: {name or template['name']}")

    return jsonify({"ok": True, "message": "Template updated."})


@bp.route("/standards/templates/<template_id>/delete", methods=["POST"])
@login_required
def template_delete(template_id):
    template = get_standards_template(template_id)
    if not template:
        return jsonify({"ok": False, "error": "Template not found."}), 404

    delete_standards_template(template_id)
    log_action(current_user(), "template_deleted",
               details=f"Template: {template['name']}")

    return jsonify({"ok": True, "message": f"Template '{template['name']}' deleted."})


@bp.route("/standards/apply", methods=["POST"])
@login_required
def standards_apply():
    """Apply a standards template to one or more tenants."""
    template_id = request.form.get("template_id", "")
    tenant_ids = request.form.getlist("tenant_ids")
    action = request.form.get("action", "report")

    if not template_id:
        return jsonify({"ok": False, "error": "Template is required."})
    if not tenant_ids:
        return jsonify({"ok": False, "error": "Select at least one tenant."})
    if action not in ("report", "alert"):
        action = "report"

    template = get_standards_template(template_id)
    if not template:
        return jsonify({"ok": False, "error": "Template not found."})

    applied = 0
    for tid in tenant_ids:
        if get_tenant(tid):
            assign_standard_to_tenant(tid, template_id, action=action,
                                      applied_by=current_user())
            applied += 1

    log_action(current_user(), "standards_applied",
               details=f"Template: {template['name']}, Tenants: {applied}, Action: {action}")

    return jsonify({
        "ok": True,
        "message": f"Applied '{template['name']}' to {applied} tenant(s) ({action} mode).",
    })
