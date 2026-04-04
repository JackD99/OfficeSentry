"""Tenant group CRUD routes."""

from flask import render_template, request, jsonify, abort, redirect, url_for, flash

from helpers import login_required, current_user, sanitize_text
from database import (
    list_tenants, log_action,
    create_tenant_group, get_tenant_group, list_tenant_groups,
    update_tenant_group, delete_tenant_group,
    add_tenant_to_group, get_group_tenants, set_group_members,
)

from routes.standards import bp
from routes.standards.definitions import GROUP_COLORS


@bp.route("/groups")
@login_required
def groups_page():
    groups = list_tenant_groups()
    tenants = list_tenants()
    return render_template("tenant_groups.html",
                           groups=groups, tenants=tenants,
                           group_colors=GROUP_COLORS)


@bp.route("/groups/new", methods=["POST"])
@login_required
def group_create():
    name = sanitize_text(request.form.get("name", ""), max_length=100)
    description = sanitize_text(request.form.get("description", ""), max_length=500)
    color = request.form.get("color", "primary")
    tenant_ids = request.form.getlist("tenant_ids")

    if not name:
        return jsonify({"ok": False, "error": "Group name is required."})

    if color not in GROUP_COLORS:
        color = "primary"

    gid = create_tenant_group(name, description=description, color=color)

    for tid in tenant_ids:
        add_tenant_to_group(gid, tid)

    log_action(current_user(), "group_created",
               details=f"Group: {name}, Members: {len(tenant_ids)}")

    return jsonify({"ok": True, "group_id": gid,
                    "message": f"Group '{name}' created with {len(tenant_ids)} tenant(s)."})


@bp.route("/groups/<group_id>")
@login_required
def group_detail(group_id):
    group = get_tenant_group(group_id)
    if not group:
        abort(404)
    members = get_group_tenants(group_id)
    all_tenants = list_tenants()
    return render_template("tenant_group_detail.html",
                           group=group, members=members,
                           all_tenants=all_tenants,
                           group_colors=GROUP_COLORS)


@bp.route("/groups/<group_id>/update", methods=["POST"])
@login_required
def group_update(group_id):
    group = get_tenant_group(group_id)
    if not group:
        return jsonify({"ok": False, "error": "Group not found."}), 404

    name = sanitize_text(request.form.get("name", ""), max_length=100)
    description = sanitize_text(request.form.get("description", ""), max_length=500)
    color = request.form.get("color", group["color"])
    tenant_ids = request.form.getlist("tenant_ids")

    fields = {}
    if name:
        fields["name"] = name
    if description is not None:
        fields["description"] = description
    if color in GROUP_COLORS:
        fields["color"] = color

    if fields:
        update_tenant_group(group_id, **fields)

    set_group_members(group_id, tenant_ids)

    log_action(current_user(), "group_updated",
               details=f"Group: {name or group['name']}, Members: {len(tenant_ids)}")

    return jsonify({"ok": True, "message": "Group updated."})


@bp.route("/groups/<group_id>/delete", methods=["POST"])
@login_required
def group_delete(group_id):
    group = get_tenant_group(group_id)
    if not group:
        return jsonify({"ok": False, "error": "Group not found."}), 404

    delete_tenant_group(group_id)
    log_action(current_user(), "group_deleted",
               details=f"Group: {group['name']}")
    flash(f"Group '{group['name']}' deleted.", "success")
    return redirect(url_for("standards.groups_page"))
