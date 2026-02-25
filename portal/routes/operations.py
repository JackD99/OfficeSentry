"""
portal/routes/operations.py — MSP bulk operations for user & mailbox management.

Provides helpdesk operations organised by tier:
  Tier 1 (1st Line): Password reset, user lookup, out-of-office, license report
  Tier 2 (2nd Line): Block/unblock users, revoke sessions, mailbox forwarding,
                      convert mailbox, MFA status, group membership
  Tier 3 (3rd Line): Full user offboarding (block + revoke + convert + remove
                      licenses + hide from GAL + set OOO + forwarding)

Each operation runs as a background PowerShell job and can target one tenant
or be fanned out across multiple tenants (bulk mode).
"""

import logging
from pathlib import Path

from flask import (
    Blueprint, render_template, request, jsonify,
)

from database import (
    get_tenant, list_tenants, log_action,
)
from helpers import (
    login_required, current_user,
    tenant_pfx_path, tenant_output_dir,
    build_ps_env, start_job, get_tenant_pfx_password,
    validate_email, sanitize_text,
    SCRIPTS_DIR, PWSH,
)

log = logging.getLogger("portal")

bp = Blueprint("operations", __name__)


# ---------------------------------------------------------------------------
# Operation definitions — metadata shared between routes and template
# ---------------------------------------------------------------------------

OPERATIONS = {
    # -- Tier 1 (1st Line — Service Desk) --
    "reset_password": {
        "tier": 1, "name": "Reset Password", "icon": "key-fill",
        "color": "primary", "script": "user",
        "desc": "Force a password reset — user must change on next sign-in.",
        "fields": ["target_user"],
    },
    "user_info": {
        "tier": 1, "name": "User Lookup", "icon": "person-vcard",
        "color": "info", "script": "report",
        "desc": "Quick view of account details, licenses, groups, and last sign-in.",
        "fields": ["target_user"],
    },
    "set_ooo": {
        "tier": 1, "name": "Set Out-of-Office", "icon": "airplane",
        "color": "success", "script": "mailbox",
        "desc": "Enable an auto-reply on a mailbox with optional date scheduling.",
        "fields": ["target_user", "ooo_message", "ooo_dates"],
    },
    "clear_ooo": {
        "tier": 1, "name": "Clear Out-of-Office", "icon": "airplane-fill",
        "color": "secondary", "script": "mailbox",
        "desc": "Disable the auto-reply on a mailbox.",
        "fields": ["target_user"],
    },
    "licenses": {
        "tier": 1, "name": "License Report", "icon": "card-checklist",
        "color": "primary", "script": "report",
        "desc": "Export all user license assignments for a tenant.",
        "fields": [],
    },

    # -- Tier 2 (2nd Line — Technical Support) --
    "block": {
        "tier": 2, "name": "Block Sign-In", "icon": "person-x",
        "color": "danger", "script": "user",
        "desc": "Immediately prevent a user from signing in to any M365 service.",
        "fields": ["target_user"],
    },
    "unblock": {
        "tier": 2, "name": "Unblock Sign-In", "icon": "person-check",
        "color": "success", "script": "user",
        "desc": "Re-enable sign-in for a previously blocked user.",
        "fields": ["target_user"],
    },
    "revoke_sessions": {
        "tier": 2, "name": "Revoke Sessions", "icon": "door-open",
        "color": "warning", "script": "user",
        "desc": "Invalidate all active sessions — forces immediate re-authentication.",
        "fields": ["target_user"],
    },
    "convert_shared": {
        "tier": 2, "name": "Convert to Shared Mailbox", "icon": "inbox",
        "color": "info", "script": "mailbox",
        "desc": "Convert a user mailbox to a shared mailbox (frees license).",
        "fields": ["target_user"],
    },
    "convert_regular": {
        "tier": 2, "name": "Convert to User Mailbox", "icon": "person-workspace",
        "color": "primary", "script": "mailbox",
        "desc": "Convert a shared mailbox back to a regular user mailbox.",
        "fields": ["target_user"],
    },
    "set_forwarding": {
        "tier": 2, "name": "Set Mail Forwarding", "icon": "forward",
        "color": "info", "script": "mailbox",
        "desc": "Forward all incoming mail to another address, optionally keeping a copy.",
        "fields": ["target_user", "forward_to", "keep_copy"],
    },
    "remove_forwarding": {
        "tier": 2, "name": "Remove Mail Forwarding", "icon": "x-circle",
        "color": "secondary", "script": "mailbox",
        "desc": "Remove any forwarding rules set on a mailbox.",
        "fields": ["target_user"],
    },
    "hide_from_gal": {
        "tier": 2, "name": "Hide from Address List", "icon": "eye-slash",
        "color": "secondary", "script": "mailbox",
        "desc": "Hide a user from the Global Address List.",
        "fields": ["target_user"],
    },
    "unhide_from_gal": {
        "tier": 2, "name": "Show in Address List", "icon": "eye",
        "color": "primary", "script": "mailbox",
        "desc": "Make a hidden user visible in the Global Address List.",
        "fields": ["target_user"],
    },
    "mfa_status": {
        "tier": 2, "name": "MFA Status Report", "icon": "shield-lock",
        "color": "warning", "script": "report",
        "desc": "Check MFA registration and authentication methods for all users.",
        "fields": [],
    },
    "group_membership": {
        "tier": 2, "name": "Group Membership", "icon": "people",
        "color": "primary", "script": "report",
        "desc": "List all groups, roles, and directory memberships for a user.",
        "fields": ["target_user"],
    },

    # -- Tier 3 (3rd Line — Engineering) --
    "offboard": {
        "tier": 3, "name": "Full User Offboard", "icon": "person-dash",
        "color": "danger", "script": "offboard",
        "desc": "Complete offboarding: block sign-in, revoke sessions, remove licenses, "
                "convert to shared mailbox, set OOO, hide from GAL.",
        "fields": ["target_user", "ooo_message", "forward_to",
                    "skip_convert", "skip_licenses", "skip_gal"],
    },
}


def _build_op_cmd(action: str, tenant: dict, tid: str, form: dict) -> list | None:
    """Build the PowerShell command list for a given operation.

    Returns the command list, or None if the action is unknown.
    """
    op = OPERATIONS.get(action)
    if not op:
        return None

    pfx_pass = get_tenant_pfx_password(tenant)
    if not pfx_pass:
        return None

    tenant_val = tenant["tenant_id"] or tenant["domain"]
    pfx_path   = str(tenant_pfx_path(tid))
    out_dir    = str(tenant_output_dir(tid))

    script_type = op["script"]

    if script_type == "user":
        # Invoke-UserAction.ps1
        cmd = [
            PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
            str(SCRIPTS_DIR / "Invoke-UserAction.ps1"),
            "-Action",          action,
            "-TargetUser",      form.get("target_user", ""),
            "-Tenant",          tenant_val,
            "-ClientId",        tenant["client_id"],
            "-CertificatePath", pfx_path,
        ]
        if action == "reset_password" and form.get("new_password"):
            cmd += ["-NewPassword", form["new_password"]]
        return cmd

    elif script_type == "mailbox":
        # Invoke-MailboxAction.ps1
        cmd = [
            PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
            str(SCRIPTS_DIR / "Invoke-MailboxAction.ps1"),
            "-Action",          action,
            "-TargetUser",      form.get("target_user", ""),
            "-Tenant",          tenant_val,
            "-ClientId",        tenant["client_id"],
            "-CertificatePath", pfx_path,
        ]
        if action == "set_ooo":
            msg = form.get("ooo_message", "")
            if msg:
                cmd += ["-OooMessage", msg]
            ext_msg = form.get("ooo_external_message", "")
            if ext_msg:
                cmd += ["-OooExternalMsg", ext_msg]
            start = form.get("ooo_start_date", "")
            end = form.get("ooo_end_date", "")
            if start and end:
                cmd += ["-OooStartDate", start, "-OooEndDate", end]
        elif action == "set_forwarding":
            fwd = form.get("forward_to", "")
            if fwd:
                cmd += ["-ForwardTo", fwd]
            if form.get("keep_copy"):
                cmd += ["-KeepCopy"]
        return cmd

    elif script_type == "report":
        # Get-UserReport.ps1
        report_map = {
            "licenses":         "licenses",
            "mfa_status":       "mfa_status",
            "user_info":        "user_info",
            "group_membership": "group_membership",
        }
        cmd = [
            PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
            str(SCRIPTS_DIR / "Get-UserReport.ps1"),
            "-ReportType",      report_map.get(action, action),
            "-Tenant",          tenant_val,
            "-ClientId",        tenant["client_id"],
            "-CertificatePath", pfx_path,
            "-OutputFolder",    out_dir,
        ]
        target = form.get("target_user", "")
        if target:
            cmd += ["-TargetUser", target]
        return cmd

    elif script_type == "offboard":
        # Invoke-UserOffboard.ps1
        cmd = [
            PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
            str(SCRIPTS_DIR / "Invoke-UserOffboard.ps1"),
            "-TargetUser",      form.get("target_user", ""),
            "-Tenant",          tenant_val,
            "-ClientId",        tenant["client_id"],
            "-CertificatePath", pfx_path,
            "-OutputFolder",    out_dir,
        ]
        msg = form.get("ooo_message", "")
        if msg:
            cmd += ["-OooMessage", msg]
        fwd = form.get("forward_to", "")
        if fwd:
            cmd += ["-ForwardTo", fwd]
        if form.get("skip_convert"):
            cmd += ["-SkipConvertMailbox"]
        if form.get("skip_licenses"):
            cmd += ["-SkipRemoveLicenses"]
        if form.get("skip_gal"):
            cmd += ["-SkipHideFromGal"]
        return cmd

    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

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
        "ooo_message":          sanitize_text(request.form.get("ooo_message", ""), 2000),
        "ooo_external_message": sanitize_text(request.form.get("ooo_external_message", ""), 2000),
        "ooo_start_date":       request.form.get("ooo_start_date", "").strip(),
        "ooo_end_date":         request.form.get("ooo_end_date", "").strip(),
        "forward_to":           forward_to,
        "keep_copy":            request.form.get("keep_copy") == "on",
        "skip_convert":         request.form.get("skip_convert") == "on",
        "skip_licenses":        request.form.get("skip_licenses") == "on",
        "skip_gal":             request.form.get("skip_gal") == "on",
    }

    cmd = _build_op_cmd(action, t, tenant_id, form)
    if not cmd:
        return jsonify({"ok": False, "error": "Failed to build command."}), 500

    env = build_ps_env(pfx_pass)
    target_label = f" ({target_user})" if target_user else ""
    label = f"{op['name']}{target_label} — {t['name']}"

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

    form = {
        "target_user":          target_user,
        "new_password":         request.form.get("new_password", "").strip(),
        "ooo_message":          sanitize_text(request.form.get("ooo_message", ""), 2000),
        "ooo_external_message": sanitize_text(request.form.get("ooo_external_message", ""), 2000),
        "ooo_start_date":       request.form.get("ooo_start_date", "").strip(),
        "ooo_end_date":         request.form.get("ooo_end_date", "").strip(),
        "forward_to":           forward_to,
        "keep_copy":            request.form.get("keep_copy") == "on",
        "skip_convert":         request.form.get("skip_convert") == "on",
        "skip_licenses":        request.form.get("skip_licenses") == "on",
        "skip_gal":             request.form.get("skip_gal") == "on",
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

        cmd = _build_op_cmd(action, t, tid, form)
        if not cmd:
            continue

        env = build_ps_env(pfx_pass)
        target_label = f" ({target_user})" if target_user else ""
        label = f"Bulk {op['name']}{target_label} — {t['name']}"

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
