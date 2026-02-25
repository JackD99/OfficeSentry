"""
portal/routes/operations.py — MSP bulk operations for user, mailbox, Teams,
Exchange, and SharePoint management.

Provides helpdesk operations organised by tier and category:

Categories:
  user       — User account management (Entra ID / Azure AD)
  exchange   — Exchange Online / mailbox management
  teams      — Microsoft Teams administration
  sharepoint — SharePoint Online site management

Tiers:
  Tier 1 (1st Line): Read-only lookups, reports, low-risk changes
  Tier 2 (2nd Line): Single-object write operations
  Tier 3 (3rd Line): Multi-step / high-impact / destructive operations

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
    validate_email, validate_site_url, sanitize_text,
    SCRIPTS_DIR, PWSH,
)

log = logging.getLogger("portal")

bp = Blueprint("operations", __name__)


# ---------------------------------------------------------------------------
# Operation definitions — metadata shared between routes and template
# ---------------------------------------------------------------------------

OPERATIONS = {
    # ===================================================================
    #  USER MANAGEMENT
    # ===================================================================

    # -- Tier 1 --
    "reset_password": {
        "tier": 1, "category": "user",
        "name": "Reset Password", "icon": "key-fill", "color": "primary",
        "script": "user",
        "desc": "Force a password reset — user must change on next sign-in.",
        "fields": ["target_user"],
    },
    "user_info": {
        "tier": 1, "category": "user",
        "name": "User Lookup", "icon": "person-vcard", "color": "info",
        "script": "report",
        "desc": "Quick view of account details, licenses, groups, and last sign-in.",
        "fields": ["target_user"],
    },
    "licenses": {
        "tier": 1, "category": "user",
        "name": "License Report", "icon": "card-checklist", "color": "primary",
        "script": "report",
        "desc": "Export all user license assignments for a tenant.",
        "fields": [],
    },
    "list_users": {
        "tier": 1, "category": "user",
        "name": "All Users Report", "icon": "people", "color": "info",
        "script": "report",
        "desc": "Export a full directory listing with sign-in activity.",
        "fields": [],
    },
    "stale_accounts": {
        "tier": 1, "category": "user",
        "name": "Stale Accounts", "icon": "clock-history", "color": "warning",
        "script": "report",
        "desc": "Find inactive accounts with no sign-in for 30/60/90+ days.",
        "fields": ["days_inactive"],
    },

    # -- Tier 2 --
    "block": {
        "tier": 2, "category": "user",
        "name": "Block Sign-In", "icon": "person-x", "color": "danger",
        "script": "user",
        "desc": "Immediately prevent a user from signing in to any M365 service.",
        "fields": ["target_user"],
    },
    "unblock": {
        "tier": 2, "category": "user",
        "name": "Unblock Sign-In", "icon": "person-check", "color": "success",
        "script": "user",
        "desc": "Re-enable sign-in for a previously blocked user.",
        "fields": ["target_user"],
    },
    "revoke_sessions": {
        "tier": 2, "category": "user",
        "name": "Revoke Sessions", "icon": "door-open", "color": "warning",
        "script": "user",
        "desc": "Invalidate all active sessions — forces immediate re-authentication.",
        "fields": ["target_user"],
    },
    "mfa_status": {
        "tier": 2, "category": "user",
        "name": "MFA Status Report", "icon": "shield-lock", "color": "warning",
        "script": "report",
        "desc": "Check MFA registration and authentication methods for all users.",
        "fields": [],
    },
    "group_membership": {
        "tier": 2, "category": "user",
        "name": "Group Membership", "icon": "diagram-3", "color": "primary",
        "script": "report",
        "desc": "List all groups, roles, and directory memberships for a user.",
        "fields": ["target_user"],
    },
    "assign_license": {
        "tier": 2, "category": "user",
        "name": "Assign License", "icon": "plus-circle", "color": "success",
        "script": "user",
        "desc": "Assign a license SKU to a user (e.g. ENTERPRISEPACK, SPE_E5).",
        "fields": ["target_user", "license_sku"],
    },
    "remove_license": {
        "tier": 2, "category": "user",
        "name": "Remove License", "icon": "dash-circle", "color": "warning",
        "script": "user",
        "desc": "Remove a specific license SKU from a user.",
        "fields": ["target_user", "license_sku"],
    },
    "update_user": {
        "tier": 2, "category": "user",
        "name": "Update User Profile", "icon": "pencil-square", "color": "info",
        "script": "user",
        "desc": "Update display name, job title, or department for a user.",
        "fields": ["target_user", "user_details"],
    },
    "invite_guest": {
        "tier": 2, "category": "user",
        "name": "Invite Guest User", "icon": "person-plus", "color": "info",
        "script": "user",
        "desc": "Send a guest invitation to an external email address.",
        "fields": ["target_user", "display_name"],
    },
    "remove_guest": {
        "tier": 2, "category": "user",
        "name": "Remove Guest User", "icon": "person-x-fill", "color": "danger",
        "script": "user",
        "desc": "Remove a guest user from the tenant directory.",
        "fields": ["target_user"],
    },

    # -- Tier 3 --
    "create_user": {
        "tier": 3, "category": "user",
        "name": "Create User", "icon": "person-plus-fill", "color": "success",
        "script": "user",
        "desc": "Create a new user account with a temporary password.",
        "fields": ["target_user", "display_name", "user_details"],
    },
    "delete_user": {
        "tier": 3, "category": "user",
        "name": "Delete User", "icon": "person-dash-fill", "color": "danger",
        "script": "user",
        "desc": "Soft-delete a user account (recoverable from recycle bin for 30 days).",
        "fields": ["target_user"],
    },
    "offboard": {
        "tier": 3, "category": "user",
        "name": "Full User Offboard", "icon": "person-dash", "color": "danger",
        "script": "offboard",
        "desc": "Complete offboarding: block sign-in, revoke sessions, remove licenses, "
                "convert to shared mailbox, set OOO, hide from GAL.",
        "fields": ["target_user", "ooo_message", "forward_to",
                    "skip_convert", "skip_licenses", "skip_gal"],
    },

    # ===================================================================
    #  EXCHANGE ONLINE
    # ===================================================================

    # -- Tier 1 --
    "set_ooo": {
        "tier": 1, "category": "exchange",
        "name": "Set Out-of-Office", "icon": "airplane", "color": "success",
        "script": "mailbox",
        "desc": "Enable an auto-reply on a mailbox with optional date scheduling.",
        "fields": ["target_user", "ooo_message", "ooo_dates"],
    },
    "clear_ooo": {
        "tier": 1, "category": "exchange",
        "name": "Clear Out-of-Office", "icon": "airplane-fill", "color": "secondary",
        "script": "mailbox",
        "desc": "Disable the auto-reply on a mailbox.",
        "fields": ["target_user"],
    },
    "mailbox_sizes": {
        "tier": 1, "category": "exchange",
        "name": "Mailbox Size Report", "icon": "hdd-stack", "color": "info",
        "script": "exchange_admin",
        "desc": "Export mailbox sizes, item counts, and last logon times for all users.",
        "fields": [],
    },
    "distro_members": {
        "tier": 1, "category": "exchange",
        "name": "Distribution Group Members", "icon": "people-fill", "color": "primary",
        "script": "exchange_admin",
        "desc": "List all members of a distribution group.",
        "fields": ["group_name"],
    },

    # -- Tier 2 --
    "convert_shared": {
        "tier": 2, "category": "exchange",
        "name": "Convert to Shared Mailbox", "icon": "inbox", "color": "info",
        "script": "mailbox",
        "desc": "Convert a user mailbox to a shared mailbox (frees license).",
        "fields": ["target_user"],
    },
    "convert_regular": {
        "tier": 2, "category": "exchange",
        "name": "Convert to User Mailbox", "icon": "person-workspace", "color": "primary",
        "script": "mailbox",
        "desc": "Convert a shared mailbox back to a regular user mailbox.",
        "fields": ["target_user"],
    },
    "set_forwarding": {
        "tier": 2, "category": "exchange",
        "name": "Set Mail Forwarding", "icon": "forward", "color": "info",
        "script": "mailbox",
        "desc": "Forward all incoming mail to another address, optionally keeping a copy.",
        "fields": ["target_user", "forward_to", "keep_copy"],
    },
    "remove_forwarding": {
        "tier": 2, "category": "exchange",
        "name": "Remove Mail Forwarding", "icon": "x-circle", "color": "secondary",
        "script": "mailbox",
        "desc": "Remove any forwarding rules set on a mailbox.",
        "fields": ["target_user"],
    },
    "hide_from_gal": {
        "tier": 2, "category": "exchange",
        "name": "Hide from Address List", "icon": "eye-slash", "color": "secondary",
        "script": "mailbox",
        "desc": "Hide a user from the Global Address List.",
        "fields": ["target_user"],
    },
    "unhide_from_gal": {
        "tier": 2, "category": "exchange",
        "name": "Show in Address List", "icon": "eye", "color": "primary",
        "script": "mailbox",
        "desc": "Make a hidden user visible in the Global Address List.",
        "fields": ["target_user"],
    },
    "add_distro_member": {
        "tier": 2, "category": "exchange",
        "name": "Add to Distribution Group", "icon": "person-plus", "color": "success",
        "script": "exchange_admin",
        "desc": "Add a user to a distribution group.",
        "fields": ["target_user", "group_name"],
    },
    "remove_distro_member": {
        "tier": 2, "category": "exchange",
        "name": "Remove from Distribution Group", "icon": "person-dash", "color": "warning",
        "script": "exchange_admin",
        "desc": "Remove a user from a distribution group.",
        "fields": ["target_user", "group_name"],
    },
    "set_mailbox_perms": {
        "tier": 2, "category": "exchange",
        "name": "Grant Mailbox Permissions", "icon": "key", "color": "info",
        "script": "exchange_admin",
        "desc": "Grant Full Access, Send As, or Send on Behalf permissions on a mailbox.",
        "fields": ["target_user", "delegate_user", "permission_type"],
    },
    "remove_mailbox_perms": {
        "tier": 2, "category": "exchange",
        "name": "Remove Mailbox Permissions", "icon": "key-fill", "color": "warning",
        "script": "exchange_admin",
        "desc": "Remove delegated mailbox permissions from a user.",
        "fields": ["target_user", "delegate_user", "permission_type"],
    },
    "set_mailbox_quota": {
        "tier": 2, "category": "exchange",
        "name": "Set Mailbox Quota", "icon": "speedometer", "color": "secondary",
        "script": "exchange_admin",
        "desc": "Set warning and send-limit quotas on a mailbox (in GB).",
        "fields": ["target_user", "quota_settings"],
    },

    # ===================================================================
    #  MICROSOFT TEAMS
    # ===================================================================

    # -- Tier 1 --
    "list_teams": {
        "tier": 1, "category": "teams",
        "name": "List All Teams", "icon": "chat-dots", "color": "primary",
        "script": "teams",
        "desc": "Export a list of all Teams with member/owner counts.",
        "fields": [],
    },
    "team_members": {
        "tier": 1, "category": "teams",
        "name": "Team Members Report", "icon": "people", "color": "info",
        "script": "teams",
        "desc": "List all members and owners of a specific team.",
        "fields": ["team_name"],
    },

    # -- Tier 2 --
    "add_team_member": {
        "tier": 2, "category": "teams",
        "name": "Add Team Member", "icon": "person-plus", "color": "success",
        "script": "teams",
        "desc": "Add a user as a member of a team.",
        "fields": ["team_name", "target_user"],
    },
    "remove_team_member": {
        "tier": 2, "category": "teams",
        "name": "Remove Team Member", "icon": "person-dash", "color": "warning",
        "script": "teams",
        "desc": "Remove a user from a team.",
        "fields": ["team_name", "target_user"],
    },
    "add_team_owner": {
        "tier": 2, "category": "teams",
        "name": "Add Team Owner", "icon": "person-gear", "color": "info",
        "script": "teams",
        "desc": "Promote a user to team owner.",
        "fields": ["team_name", "target_user"],
    },

    # -- Tier 3 --
    "archive_team": {
        "tier": 3, "category": "teams",
        "name": "Archive Team", "icon": "archive", "color": "warning",
        "script": "teams",
        "desc": "Archive a team — makes it read-only for all members.",
        "fields": ["team_name"],
    },
    "unarchive_team": {
        "tier": 3, "category": "teams",
        "name": "Unarchive Team", "icon": "archive-fill", "color": "success",
        "script": "teams",
        "desc": "Restore an archived team to active status.",
        "fields": ["team_name"],
    },

    # ===================================================================
    #  SHAREPOINT ONLINE
    # ===================================================================

    # -- Tier 1 --
    "list_sites": {
        "tier": 1, "category": "sharepoint",
        "name": "List All Sites", "icon": "diagram-3", "color": "primary",
        "script": "spo_admin",
        "desc": "Export all site collections with storage and sharing settings.",
        "fields": [],
    },
    "site_storage": {
        "tier": 1, "category": "sharepoint",
        "name": "Site Storage Report", "icon": "hdd", "color": "info",
        "script": "spo_admin",
        "desc": "Storage usage report for all sites — flags sites above 75% and 90%.",
        "fields": [],
    },

    # -- Tier 2 --
    "add_site_admin": {
        "tier": 2, "category": "sharepoint",
        "name": "Add Site Admin", "icon": "person-plus", "color": "success",
        "script": "spo_admin",
        "desc": "Add a user as a site collection administrator.",
        "fields": ["site_url", "target_user"],
    },
    "remove_site_admin": {
        "tier": 2, "category": "sharepoint",
        "name": "Remove Site Admin", "icon": "person-dash", "color": "warning",
        "script": "spo_admin",
        "desc": "Remove a site collection administrator.",
        "fields": ["site_url", "target_user"],
    },
    "set_site_sharing": {
        "tier": 2, "category": "sharepoint",
        "name": "Set Site Sharing", "icon": "share", "color": "info",
        "script": "spo_admin",
        "desc": "Change external sharing settings for a specific site.",
        "fields": ["site_url", "sharing_level"],
    },

    # -- Tier 3 --
    "lock_site": {
        "tier": 3, "category": "sharepoint",
        "name": "Lock Site (Read-Only)", "icon": "lock", "color": "danger",
        "script": "spo_admin",
        "desc": "Lock a site collection to read-only mode.",
        "fields": ["site_url"],
    },
    "unlock_site": {
        "tier": 3, "category": "sharepoint",
        "name": "Unlock Site", "icon": "unlock", "color": "success",
        "script": "spo_admin",
        "desc": "Unlock a previously locked site collection.",
        "fields": ["site_url"],
    },
}

# Category metadata for the template
CATEGORIES = {
    "all":        {"name": "All Operations",  "icon": "grid-3x3-gap"},
    "user":       {"name": "User Management", "icon": "person-badge"},
    "exchange":   {"name": "Exchange Online",  "icon": "envelope"},
    "teams":      {"name": "Microsoft Teams",  "icon": "chat-dots"},
    "sharepoint": {"name": "SharePoint",       "icon": "folder2-open"},
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
        if action in ("create_user", "invite_guest", "update_user"):
            dn = form.get("display_name", "")
            if dn:
                cmd += ["-DisplayName", dn]
        if action in ("create_user", "update_user"):
            jt = form.get("job_title", "")
            if jt:
                cmd += ["-JobTitle", jt]
            dept = form.get("department", "")
            if dept:
                cmd += ["-Department", dept]
        if action in ("assign_license", "remove_license"):
            sku = form.get("license_sku", "")
            if sku:
                cmd += ["-LicenseSku", sku]
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
            "list_users":       "list_users",
            "stale_accounts":   "stale_accounts",
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
        if action == "stale_accounts":
            days = form.get("days_inactive", "90")
            cmd += ["-DaysInactive", str(days)]
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

    elif script_type == "teams":
        # Invoke-TeamAction.ps1
        cmd = [
            PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
            str(SCRIPTS_DIR / "Invoke-TeamAction.ps1"),
            "-Action",          action,
            "-Tenant",          tenant_val,
            "-ClientId",        tenant["client_id"],
            "-CertificatePath", pfx_path,
        ]
        if out_dir:
            cmd += ["-OutputFolder", out_dir]
        team = form.get("team_name", "")
        if team:
            cmd += ["-TeamName", team]
        target = form.get("target_user", "")
        if target:
            cmd += ["-TargetUser", target]
        return cmd

    elif script_type == "exchange_admin":
        # Invoke-ExchangeAdmin.ps1
        cmd = [
            PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
            str(SCRIPTS_DIR / "Invoke-ExchangeAdmin.ps1"),
            "-Action",          action,
            "-Tenant",          tenant_val,
            "-ClientId",        tenant["client_id"],
            "-CertificatePath", pfx_path,
        ]
        if out_dir:
            cmd += ["-OutputFolder", out_dir]
        target = form.get("target_user", "")
        if target:
            cmd += ["-TargetUser", target]
        group = form.get("group_name", "")
        if group:
            cmd += ["-GroupName", group]
        delegate = form.get("delegate_user", "")
        if delegate:
            cmd += ["-DelegateUser", delegate]
        perm = form.get("permission_type", "")
        if perm:
            cmd += ["-PermissionType", perm]
        qw = form.get("quota_warning_gb", "")
        if qw:
            cmd += ["-QuotaWarningGB", qw]
        ql = form.get("quota_limit_gb", "")
        if ql:
            cmd += ["-QuotaLimitGB", ql]
        return cmd

    elif script_type == "spo_admin":
        # Invoke-SPOAdmin.ps1
        cmd = [
            PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
            str(SCRIPTS_DIR / "Invoke-SPOAdmin.ps1"),
            "-Action",          action,
            "-Tenant",          tenant_val,
            "-ClientId",        tenant["client_id"],
            "-CertificatePath", pfx_path,
        ]
        if out_dir:
            cmd += ["-OutputFolder", out_dir]
        site = form.get("site_url", "")
        if site:
            cmd += ["-SiteUrl", site]
        target = form.get("target_user", "")
        if target:
            cmd += ["-TargetUser", target]
        sharing = form.get("sharing_level", "")
        if sharing:
            cmd += ["-SharingLevel", sharing]
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
