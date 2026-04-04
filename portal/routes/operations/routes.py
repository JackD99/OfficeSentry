"""Operations page, single-run, and bulk execution routes."""

from flask import render_template, request, jsonify

from database import get_tenant, list_tenants, log_action
from helpers import (
    login_required, current_user,
    tenant_pfx_path,
    build_ps_env, start_job, start_python_job, get_tenant_pfx_password,
    validate_email, validate_site_url, sanitize_text,
)

from routes.operations import bp
from routes.operations.definitions import OPERATIONS, CATEGORIES
from routes.operations.commands import build_op_cmd


@bp.route("/operations")
@login_required
def operations_page():
    tenants = list_tenants()
    ready_tenants = [t for t in tenants if t["client_id"]]

    tier1 = {k: v for k, v in OPERATIONS.items() if v["tier"] == 1}
    tier2 = {k: v for k, v in OPERATIONS.items() if v["tier"] == 2}
    tier3 = {k: v for k, v in OPERATIONS.items() if v["tier"] == 3}

    return render_template(
        "operations.html",
        tenants=ready_tenants,
        tier1=tier1,
        tier2=tier2,
        tier3=tier3,
        operations=OPERATIONS,
        categories=CATEGORIES,
    )


@bp.route("/operations/run", methods=["POST"])
@login_required
def operations_run():
    """Execute a single operation on one tenant."""
    action    = request.form.get("action", "").strip()
    tenant_id = request.form.get("tenant_id", "").strip()

    if action not in OPERATIONS:
        return jsonify({"ok": False, "error": "Unknown operation."}), 400

    op = OPERATIONS[action]

    # RBAC tier check
    from database import get_user
    from permissions import can_run_operation
    user = get_user(current_user())
    if user and not can_run_operation(user, op.get("tier", 1)):
        return jsonify({"ok": False, "error": "Insufficient permissions for this operation tier."}), 403

    # Validate required target_user field
    target_user = request.form.get("target_user", "").strip()
    if "target_user" in op["fields"] and not target_user:
        return jsonify({"ok": False, "error": "Target user email is required."}), 400
    if target_user and not validate_email(target_user):
        return jsonify({"ok": False, "error": "Invalid email address format."}), 400

    # Validate forwarding address if needed
    forward_to = request.form.get("forward_to", "").strip()
    if "forward_to" in op["fields"] and action == "set_forwarding" and not forward_to:
        return jsonify({"ok": False, "error": "Forwarding address is required."}), 400
    if forward_to and not validate_email(forward_to):
        return jsonify({"ok": False, "error": "Invalid forwarding email address."}), 400

    # Validate delegate user if needed
    delegate_user = request.form.get("delegate_user", "").strip()
    if delegate_user and not validate_email(delegate_user):
        return jsonify({"ok": False, "error": "Invalid delegate email address."}), 400

    # Validate site URL if needed
    site_url = request.form.get("site_url", "").strip()
    if "site_url" in op["fields"] and not site_url:
        return jsonify({"ok": False, "error": "SharePoint site URL is required."}), 400
    if site_url and not validate_site_url(site_url):
        return jsonify({"ok": False, "error": "Invalid SharePoint site URL."}), 400

    # Validate team name if needed
    team_name = request.form.get("team_name", "").strip()
    if "team_name" in op["fields"] and not team_name:
        return jsonify({"ok": False, "error": "Team name is required."}), 400

    # Validate group name if needed
    group_name = request.form.get("group_name", "").strip()
    if "group_name" in op["fields"] and not group_name:
        return jsonify({"ok": False, "error": "Group name is required."}), 400

    t = get_tenant(tenant_id)
    if not t:
        return jsonify({"ok": False, "error": "Tenant not found."}), 404
    if not t["client_id"]:
        return jsonify({"ok": False, "error": "Tenant is not configured (no client ID)."}), 400

    pfx_pass = get_tenant_pfx_password(t)
    if not pfx_pass:
        return jsonify({"ok": False, "error": "PFX password not configured."}), 400

    form = {
        "target_user":          target_user,
        "new_password":         request.form.get("new_password", "").strip(),
        "display_name":         sanitize_text(request.form.get("display_name", ""), 200),
        "job_title":            sanitize_text(request.form.get("job_title", ""), 200),
        "department":           sanitize_text(request.form.get("department", ""), 200),
        "license_sku":          sanitize_text(request.form.get("license_sku", ""), 100),
        "ooo_message":          sanitize_text(request.form.get("ooo_message", ""), 2000),
        "ooo_external_message": sanitize_text(request.form.get("ooo_external_message", ""), 2000),
        "ooo_start_date":       request.form.get("ooo_start_date", "").strip(),
        "ooo_end_date":         request.form.get("ooo_end_date", "").strip(),
        "forward_to":           forward_to,
        "keep_copy":            request.form.get("keep_copy") == "on",
        "skip_convert":         request.form.get("skip_convert") == "on",
        "skip_licenses":        request.form.get("skip_licenses") == "on",
        "skip_gal":             request.form.get("skip_gal") == "on",
        "team_name":            sanitize_text(team_name, 300),
        "group_name":           sanitize_text(group_name, 300),
        "delegate_user":        delegate_user,
        "permission_type":      request.form.get("permission_type", "").strip(),
        "quota_warning_gb":     request.form.get("quota_warning_gb", "").strip(),
        "quota_limit_gb":       request.form.get("quota_limit_gb", "").strip(),
        "site_url":             site_url,
        "sharing_level":        request.form.get("sharing_level", "").strip(),
        "days_inactive":        request.form.get("days_inactive", "90").strip(),
    }

    target_label = f" ({target_user})" if target_user else ""
    label = f"{op['name']}{target_label} — {t['name']}"

    # User report operations use native Python (Graph API)
    if op["script"] == "report":
        from graph_jobs import fetch_user_report
        pfx_path = str(tenant_pfx_path(tenant_id))
        kwargs = {
            "report_type": action,
            "target_user": target_user,
        }
        if action == "stale_accounts":
            try:
                kwargs["days_inactive"] = int(form.get("days_inactive", "90"))
            except ValueError:
                kwargs["days_inactive"] = 90
        job_id = start_python_job(
            label, "operations",
            fetch_user_report,
            args=(dict(t), pfx_path, pfx_pass),
            kwargs=kwargs,
            tenant_id=tenant_id,
        )
    else:
        cmd = build_op_cmd(action, t, tenant_id, form)
        if not cmd:
            return jsonify({"ok": False, "error": "Failed to build command."}), 500

        env = build_ps_env(pfx_pass)
        job_id = start_job(label, "operations", cmd, tenant_id=tenant_id, env=env)

    log_action(
        current_user(), f"op_{action}",
        tenant_id=tenant_id, tenant_name=t["name"],
        details=f"Target: {target_user or 'all'}"
    )

    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/operations/bulk", methods=["POST"])
@login_required
def operations_bulk():
    """Execute an operation across multiple tenants (bulk mode)."""
    action     = request.form.get("action", "").strip()
    tenant_ids = request.form.getlist("tenant_ids")

    if action not in OPERATIONS:
        return jsonify({"ok": False, "error": "Unknown operation."}), 400
    if not tenant_ids:
        return jsonify({"ok": False, "error": "No tenants selected."}), 400

    op = OPERATIONS[action]

    # Validate required target_user field
    target_user = request.form.get("target_user", "").strip()
    if "target_user" in op["fields"] and not target_user:
        return jsonify({"ok": False, "error": "Target user email is required."}), 400
    if target_user and not validate_email(target_user):
        return jsonify({"ok": False, "error": "Invalid email address format."}), 400

    # Validate forwarding address
    forward_to = request.form.get("forward_to", "").strip()
    if forward_to and not validate_email(forward_to):
        return jsonify({"ok": False, "error": "Invalid forwarding email address."}), 400

    # Validate delegate user
    delegate_user = request.form.get("delegate_user", "").strip()
    if delegate_user and not validate_email(delegate_user):
        return jsonify({"ok": False, "error": "Invalid delegate email address."}), 400

    # Validate site URL
    site_url = request.form.get("site_url", "").strip()
    if site_url and not validate_site_url(site_url):
        return jsonify({"ok": False, "error": "Invalid SharePoint site URL."}), 400

    form = {
        "target_user":          target_user,
        "new_password":         request.form.get("new_password", "").strip(),
        "display_name":         sanitize_text(request.form.get("display_name", ""), 200),
        "job_title":            sanitize_text(request.form.get("job_title", ""), 200),
        "department":           sanitize_text(request.form.get("department", ""), 200),
        "license_sku":          sanitize_text(request.form.get("license_sku", ""), 100),
        "ooo_message":          sanitize_text(request.form.get("ooo_message", ""), 2000),
        "ooo_external_message": sanitize_text(request.form.get("ooo_external_message", ""), 2000),
        "ooo_start_date":       request.form.get("ooo_start_date", "").strip(),
        "ooo_end_date":         request.form.get("ooo_end_date", "").strip(),
        "forward_to":           forward_to,
        "keep_copy":            request.form.get("keep_copy") == "on",
        "skip_convert":         request.form.get("skip_convert") == "on",
        "skip_licenses":        request.form.get("skip_licenses") == "on",
        "skip_gal":             request.form.get("skip_gal") == "on",
        "team_name":            sanitize_text(request.form.get("team_name", ""), 300),
        "group_name":           sanitize_text(request.form.get("group_name", ""), 300),
        "delegate_user":        delegate_user,
        "permission_type":      request.form.get("permission_type", "").strip(),
        "quota_warning_gb":     request.form.get("quota_warning_gb", "").strip(),
        "quota_limit_gb":       request.form.get("quota_limit_gb", "").strip(),
        "site_url":             site_url,
        "sharing_level":        request.form.get("sharing_level", "").strip(),
        "days_inactive":        request.form.get("days_inactive", "90").strip(),
    }

    job_ids = []
    tenant_jobs = []

    for tid in tenant_ids:
        t = get_tenant(tid)
        if not t or not t["client_id"]:
            continue

        pfx_pass = get_tenant_pfx_password(t)
        if not pfx_pass:
            continue

        target_label = f" ({target_user})" if target_user else ""
        label = f"Bulk {op['name']}{target_label} — {t['name']}"

        if op["script"] == "report":
            from graph_jobs import fetch_user_report
            pfx_path = str(tenant_pfx_path(tid))
            kwargs = {"report_type": action, "target_user": target_user}
            if action == "stale_accounts":
                try:
                    kwargs["days_inactive"] = int(form.get("days_inactive", "90"))
                except ValueError:
                    kwargs["days_inactive"] = 90
            jid = start_python_job(
                label, "operations",
                fetch_user_report,
                args=(dict(t), pfx_path, pfx_pass),
                kwargs=kwargs,
                tenant_id=tid,
            )
        else:
            cmd = build_op_cmd(action, t, tid, form)
            if not cmd:
                continue
            env = build_ps_env(pfx_pass)
            jid = start_job(label, "operations", cmd, tenant_id=tid, env=env)
        job_ids.append(jid)
        tenant_jobs.append({
            "tenant_id": tid,
            "tenant_name": t["name"],
            "job_id": jid,
        })

    log_action(
        current_user(), f"bulk_op_{action}",
        details=f"Action: {action}, Target: {target_user or 'all'}, Tenants: {len(job_ids)}"
    )

    return jsonify({
        "ok": True,
        "job_ids": job_ids,
        "tenant_jobs": tenant_jobs,
        "message": f"Started {len(job_ids)} {op['name']} job(s).",
    })
