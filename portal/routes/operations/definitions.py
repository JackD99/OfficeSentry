"""Operation and category definitions — metadata shared between routes and templates."""

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

}

# Category metadata for the template
CATEGORIES = {
    "all":        {"name": "All Operations",  "icon": "grid-3x3-gap"},
    "user":       {"name": "User Management", "icon": "person-badge"},
    "teams":      {"name": "Microsoft Teams",  "icon": "chat-dots"},
}
