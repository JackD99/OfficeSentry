"""BPA check definitions and shared constants."""

BPA_CHECKS = [
    {
        "key": "unified_audit_log",
        "name": "Unified Audit Log",
        "category": "Security",
        "description": "Verifies that tenant-level audit logging is enabled. Essential for compliance, security monitoring, and incident response.",
        "expected": "Enabled",
        "impact": "Critical",
    },
    {
        "key": "security_defaults",
        "name": "Security Defaults",
        "category": "Identity",
        "description": "Determines whether secure defaults are active, enforcing MFA and blocking legacy authentication.",
        "expected": "Enabled (or Conditional Access)",
        "impact": "High",
    },
    {
        "key": "mfa_registration",
        "name": "MFA Registration Campaign",
        "category": "Identity",
        "description": "Assesses if MFA registration nudges are enabled to encourage multi-factor authentication adoption.",
        "expected": "Enabled",
        "impact": "High",
    },
    {
        "key": "password_expiry",
        "name": "Password Expiry Policy",
        "category": "Identity",
        "description": "Evaluates whether password policies enforce expiry. NIST recommends no forced expiry when MFA is in use.",
        "expected": "No forced expiry (with MFA)",
        "impact": "Medium",
    },
    {
        "key": "self_service_password_reset",
        "name": "Self-Service Password Reset",
        "category": "Identity",
        "description": "Checks if SSPR is enabled for all users to reduce helpdesk burden while maintaining security.",
        "expected": "Enabled for all users",
        "impact": "Medium",
    },
    {
        "key": "admin_mfa",
        "name": "Global Admin MFA",
        "category": "Identity",
        "description": "Verifies that all Global Administrators have MFA enabled. Unprotected admin accounts are a critical risk.",
        "expected": "All admins MFA-enabled",
        "impact": "Critical",
    },
    {
        "key": "oauth_app_consent",
        "name": "OAuth App Consent",
        "category": "Security",
        "description": "Ensures OAuth app consent policies restrict unauthorized data access and prevent consent phishing.",
        "expected": "Admin consent required",
        "impact": "High",
    },
    {
        "key": "legacy_auth_blocked",
        "name": "Legacy Authentication",
        "category": "Identity",
        "description": "Checks whether legacy authentication protocols are blocked. Legacy auth bypasses MFA.",
        "expected": "Blocked",
        "impact": "High",
    },
    {
        "key": "mailbox_auditing",
        "name": "Mailbox Auditing",
        "category": "Exchange",
        "description": "Verifies that mailbox auditing is enabled (default since January 2019).",
        "expected": "Enabled by default",
        "impact": "Medium",
    },
    {
        "key": "external_forwarding",
        "name": "External Email Forwarding",
        "category": "Exchange",
        "description": "Checks if auto-forwarding to external recipients is blocked to prevent data exfiltration.",
        "expected": "Blocked",
        "impact": "High",
    },
    {
        "key": "sharepoint_external_sharing",
        "name": "SharePoint External Sharing",
        "category": "SharePoint",
        "description": "Reviews SharePoint external sharing settings to ensure appropriate access controls.",
        "expected": "Restricted or disabled",
        "impact": "High",
    },
    {
        "key": "onedrive_external_sharing",
        "name": "OneDrive External Sharing",
        "category": "SharePoint",
        "description": "Reviews OneDrive external sharing settings for appropriate restrictions.",
        "expected": "Restricted",
        "impact": "Medium",
    },
    {
        "key": "secure_score",
        "name": "Microsoft Secure Score",
        "category": "Overall",
        "description": "Retrieves and evaluates the tenant's Microsoft Secure Score as a measure of overall security posture.",
        "expected": ">= 70%",
        "impact": "High",
    },
]

IMPACT_COLORS = {
    "Critical": "danger",
    "High": "warning",
    "Medium": "info",
    "Low": "primary",
}

# Colors available for tenant groups
GROUP_COLORS = [
    "primary", "success", "danger", "warning", "info", "secondary",
    "light", "dark",
]
