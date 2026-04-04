"""PowerShell command builder for operations."""

from helpers import (
    tenant_pfx_path, tenant_output_dir,
    SCRIPTS_DIR, PWSH,
)

from routes.operations.definitions import OPERATIONS


def build_op_cmd(action: str, tenant: dict, tid: str, form: dict) -> list | None:
    """Build the PowerShell command list for a given operation.

    Returns the command list, or None if the action is unknown.
    Caller is responsible for validating pfx password exists before calling.
    """
    op = OPERATIONS.get(action)
    if not op:
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

    return None
