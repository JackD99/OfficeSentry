"""Cyber insurance proposals and supplier security questionnaires, judged from Microsoft 365 data.

Neither is a certification: an insurer prices the risk and a client weighs the answers, so a gap on a
few accounts is "Partly" rather than a Fail, and the point is the text to paste. Each control is a question UK proposal
forms and due-diligence questionnaires usually ask, judged from the checks where Microsoft 365 can see it
and from the engineer's saved answer where it can't, with the form's own wording in the answers below.
"""

from __future__ import annotations

from . import evidence_topics  # noqa: F401  (the topics the controls name must be registered first)
from .schemes import Control, Scheme, Theme, figures_admins, figures_devices, figures_mfa_users, register

OUTSIDE = "Not something Microsoft 365 shows; answer from the client's own records."
M365_ONLY = "Judged for Microsoft 365 sign-ins only. Add VPNs, remote desktop and other systems."

ACCESS = Theme(
    "access",
    "Multi-factor authentication and access",
    "Every account signs in with MFA, legacy protocols that can't are blocked, administrators are few, "
    "separate and named, and accounts go when people do.",
    (
        Control(
            "mfa_all",
            "",
            "MFA for every user, on email and cloud apps",
            "Multi-factor authentication is enforced for all users accessing email (including webmail and "
            "mobile) and cloud applications.",
            topic="mfa_remote",
            support=("users_mfa_registered",),
            report="mfa",
            figures=figures_mfa_users,
            scope=M365_ONLY,
        ),
        Control(
            "mfa_admins",
            "",
            "MFA for every administrator",
            "Every privileged and administrator account is protected by MFA.",
            topic="mfa_privileged",
            support=("admin_mfa",),
            report="admins",
            figures=figures_admins,
            scope="Judged for Microsoft 365 and Entra ID admin roles. Add administrators of other systems.",
        ),
        Control(
            "remote_access",
            "",
            "MFA on remote access",
            "VPN, remote desktop and remote support tools need MFA.",
            topic="remote_access",
            scope=OUTSIDE,
        ),
        Control(
            "legacy_auth",
            "",
            "Legacy authentication blocked",
            "Sign-in protocols that can't use MFA (IMAP, POP, SMTP AUTH, older Office clients) are blocked.",
            rules=("legacy_auth_blocked", "smtp_auth"),
            topic="legacy_auth",
            report="legacy-auth",
            scope="Judged from Conditional Access and Exchange Online settings.",
        ),
        Control(
            "separate_admins",
            "",
            "Separate accounts for administration",
            "Administrators use a separate account for admin work, not the one they read email with.",
            topic="separate_admins",
            report="admins",
            fix="Give each administrator a separate, unlicensed admin account with no mailbox, used only for "
            "administration, and take the admin roles off the account they use every day.",
            scope="Judged from Microsoft 365 admin role holders: an admin account with a licence or a mailbox "
            "looks like an everyday account.",
        ),
        Control(
            "privileged",
            "",
            "Few, named, internal administrators",
            "Administrator access is limited to a small number of named internal people: no guests, no "
            "unnecessary Global Administrators, no apps with admin roles.",
            rules=("global_admin_count", "guest_admins", "service_principal_admins", "cloud_only_admins"),
            topic="admin_count",
            report="admins",
            figures=figures_admins,
            scope="Microsoft 365 shows who holds admin roles today; describe how the list is reviewed.",
        ),
        Control(
            "accounts",
            "",
            "Leavers and inactive accounts removed",
            "Accounts are disabled promptly when people leave, inactive accounts are removed, and no "
            "disabled account keeps a licence.",
            rules=("stale_users", "licensed_disabled", "pending_guests"),
            topic="leavers",
            report="inactive",
            scope="Judged from sign-in activity in Microsoft Entra ID (needs Entra ID P1 for last sign-in "
            "dates). Describe the leavers process in the answer.",
        ),
    ),
)

EMAIL = Theme(
    "email",
    "Email security",
    "Mail is filtered, the domains can't be spoofed, nothing forwards outside unnoticed, and payment "
    "changes are verified by voice.",
    (
        Control(
            "email_filtering",
            "",
            "Incoming mail filtered",
            "Incoming email is filtered for spam, phishing and malicious links and attachments.",
            topic="email_filtering",
            scope="Microsoft 365 shows which mail gateway the domains route through, not its settings.",
        ),
        Control(
            "email_auth",
            "",
            "SPF, DKIM and DMARC on every domain",
            "Every email domain publishes SPF, signs with DKIM and has a DMARC policy.",
            rules=("spf_record", "dkim_enabled", "dmarc_policy"),
            topic="email_auth",
            report="domains",
            scope="Judged from public DNS and Exchange Online for the tenant's domains.",
        ),
        Control(
            "forwarding",
            "",
            "No automatic forwarding outside",
            "Automatic forwarding of email to external addresses is blocked, and inbox rules that forward or "
            "hide mail are caught.",
            rules=("auto_forward_policy", "external_forwarding", "suspicious_inbox_rules"),
            topic="forwarding",
            report="inbox-rules",
            scope="Judged from the Exchange Online outbound spam policy, mailbox forwarding and inbox rules.",
        ),
        Control(
            "payment_verification",
            "",
            "Payment changes verified by phone",
            "Requests to change bank details or make payments are verified with a known contact by phone.",
            topic="payment_verification",
            scope=OUTSIDE,
        ),
    ),
)

DEVICES = Theme(
    "devices",
    "Devices and patching",
    "Computers are managed, encrypted, protected by EDR, on supported operating systems and patched within 14 days.",
    (
        Control(
            "edr",
            "",
            "EDR on every computer and server",
            "Endpoint detection and response is installed and monitored on all computers and servers.",
            topic="edr",
            scope="Office Sentry doesn't read Microsoft Defender; answer from the EDR console.",
        ),
        Control(
            "devices",
            "",
            "Managed, encrypted and on supported systems",
            "Company devices are managed, encrypted and run operating systems that still get security updates.",
            rules=("device_encryption", "device_compliance", "supported_windows"),
            topic="devices_patched",
            report="devices",
            figures=figures_devices,
            scope="Judged for devices known to Intune or Entra ID. Servers, macOS and unmanaged devices aren't seen.",
        ),
        Control(
            "patching_14_days",
            "",
            "Critical updates within 14 days",
            "Critical security updates are installed within 14 days of release (some forms say 30).",
            topic="patching_14_days",
            support=("device_checkin",),
            scope="Intune shows when each device last checked in, not when updates were installed.",
        ),
    ),
)

RECOVERY = Theme(
    "recovery",
    "Backups and recovery",
    "Data is backed up separately and the restores are tested; there is a plan for an incident and for "
    "keeping the business running.",
    (
        Control(
            "backups",
            "",
            "Backups kept separately and tested",
            "Backups are taken regularly, kept offline or immutable and separate from the main systems, and "
            "restores are tested.",
            topic="backups",
            scope=OUTSIDE,
        ),
        Control(
            "bcp_dr",
            "",
            "Continuity and recovery plan",
            "A business continuity and disaster recovery plan exists and is tested.",
            topic="bcp_dr",
            scope=OUTSIDE,
        ),
        Control(
            "incident_response",
            "",
            "Incident response plan",
            "A documented incident response plan says who does what.",
            topic="incident_response",
            scope=OUTSIDE,
        ),
    ),
)

MONITORING = Theme(
    "monitoring",
    "Monitoring and data",
    "Activity is logged, suspicious sign-ins are watched, and data only leaves through sharing and apps "
    "that are controlled.",
    (
        Control(
            "logging",
            "",
            "Audit logging on",
            "Audit logging is enabled for email and user activity.",
            rules=("mailbox_auditing",),
            topic="audit_logs",
            report="tenant-settings",
            scope="Judged from Exchange Online mailbox auditing and the unified audit log.",
        ),
        Control(
            "risk_detection",
            "",
            "Suspicious sign-ins monitored",
            "Risky sign-ins and users are detected and acted on.",
            rules=("risky_users", "risky_sign_ins"),
            topic="risk_detection",
            report="risky-users",
            scope="Judged from Entra ID Protection (needs Entra ID P2 for the full detections).",
        ),
        Control(
            "sharing",
            "",
            "External sharing restricted",
            "Sharing files with people outside the organisation is restricted and guests are invited deliberately.",
            rules=("sharepoint_sharing", "anyone_links", "guest_invites"),
            topic="external_sharing",
            report="shared-files",
            scope="Judged from SharePoint and OneDrive sharing settings and links.",
        ),
        Control(
            "apps",
            "",
            "Third-party apps controlled",
            "Users can't consent to third-party apps on their own, and apps with risky permissions are known.",
            rules=("user_consent", "risky_app_permissions"),
            topic="third_party_apps",
            report="apps",
            scope="Judged from Entra ID consent settings and app permissions.",
        ),
    ),
)

NETWORK = Theme(
    "network",
    "Network and people",
    "A firewall between the network and the internet, no remote desktop open to it, and staff trained to "
    "spot phishing.",
    (
        Control(
            "firewall_boundary",
            "",
            "Firewalls at the internet boundary",
            "Firewalls sit between the network and the internet.",
            topic="firewall_boundary",
            scope=OUTSIDE,
        ),
        Control(
            "rdp_exposed",
            "",
            "No remote desktop open to the internet",
            "Remote desktop is closed to the internet, or only reachable through a VPN or gateway with MFA.",
            topic="rdp_exposed",
            scope=OUTSIDE,
        ),
        Control(
            "training",
            "",
            "Security awareness training",
            "Staff receive regular security awareness and phishing training.",
            topic="training",
            scope=OUTSIDE,
        ),
    ),
)

INSURANCE = register(
    Scheme(
        "insurance",
        "Cyber insurance proposal",
        "Cyber insurance",
        "The controls UK cyber insurance proposal and renewal forms ask about, with the answers to paste.",
        "forms",
        "What insurers price the premium on, judged from Microsoft 365 where it can see it. A control missing "
        "for the whole organisation is a Fail and a gap on a few accounts is Partly; insurers don't refuse an "
        "application for either, they price it or add conditions, so declare both. Match each answer to the "
        "question on the form, since wording differs between insurers.",
        (ACCESS, EMAIL, DEVICES, RECOVERY, MONITORING, NETWORK),
        "UK cyber insurance proposal forms as commonly worded in 2026",
        "Questions drawn from the proposal and renewal forms of UK cyber insurers and brokers, in their "
        "typical wording; no insurer's form is copied.",
        "",
        strict=False,
        questions="insurance",
        notes=(
            "Scope: insurers ask about the whole business, not one cloud service. A Pass here covers Microsoft "
            "365; the answer should say what covers the rest.",
            "Most forms are signed as a warranty: a wrong answer can void the policy, so a Partly should be "
            "declared, not rounded up.",
        ),
        renewal="12 months",
    )
)

GOVERNANCE = Theme(
    "governance",
    "Governance",
    "Written policies and recognised certifications.",
    (
        Control(
            "policies",
            "",
            "Information security policies",
            "Documented information security policies exist and are reviewed.",
            topic="policies",
            scope=OUTSIDE,
        ),
        Control(
            "certifications",
            "",
            "Security certifications held",
            "Cyber Essentials, CE Plus, ISO 27001 or similar, with dates.",
            topic="certifications",
            scope=OUTSIDE,
        ),
    ),
)

S_ACCESS = Theme(
    "access",
    "Access control",
    "Unique accounts with MFA, phishing-resistant for administrators, separate admin accounts, a password "
    "policy, and access removed when people leave.",
    (
        Control(
            "mfa_all",
            "",
            "MFA for every user",
            "Multi-factor authentication is required for all users.",
            topic="mfa_remote",
            support=("users_mfa_registered",),
            report="mfa",
            figures=figures_mfa_users,
            scope=M365_ONLY,
        ),
        Control(
            "mfa_admins",
            "",
            "MFA for privileged accounts",
            "Every privileged account uses MFA.",
            topic="mfa_privileged",
            support=("admin_mfa",),
            report="admins",
            figures=figures_admins,
            scope="Judged for Microsoft 365 and Entra ID admin roles.",
        ),
        Control(
            "admins_phishing_resistant",
            "",
            "Phishing-resistant MFA for administrators",
            "Administrators sign in with FIDO2 keys, passkeys or Windows Hello for Business, not codes.",
            rules=("admin_phishing_resistant",),
            topic="admins_phishing_resistant",
            report="auth-methods",
            scope="Judged from the sign-in methods admin accounts have registered.",
        ),
        Control(
            "mfa_methods",
            "",
            "No weak MFA methods",
            "Text-message and voice-call MFA are turned off or being retired.",
            rules=("phone_only_mfa",),
            topic="mfa_methods",
            report="auth-methods",
            scope="Judged from the authentication methods policy and what people have registered.",
        ),
        Control(
            "separate_admins",
            "",
            "Separate admin accounts",
            "Administrative accounts are separate from standard user accounts.",
            topic="separate_admins",
            report="admins",
            fix="Give each administrator a separate, unlicensed admin account with no mailbox, used only for "
            "administration, and take the admin roles off the account they use every day.",
            scope="Judged from Microsoft 365 admin role holders: an admin account with a licence or a mailbox "
            "looks like an everyday account.",
        ),
        Control(
            "privileged",
            "",
            "Privileged access restricted and tracked",
            "Few named administrators, no guests or apps with admin roles, and the list is reviewed.",
            rules=("global_admin_count", "guest_admins", "service_principal_admins", "cloud_only_admins"),
            topic="admin_count",
            report="admins",
            figures=figures_admins,
            scope="Microsoft 365 shows who holds admin roles today; describe how the list is reviewed.",
        ),
        Control(
            "shared_accounts",
            "",
            "Unique accounts, no shared logins",
            "Every user has their own account; shared mailboxes can't be signed in to.",
            rules=("shared_mailbox_signin",),
            topic="shared_accounts",
            report="shared-mailboxes",
            scope="Judged from whether shared mailboxes can be signed in to. Check devices and other systems.",
        ),
        Control(
            "password_policy",
            "",
            "Password policy",
            "Passwords have a minimum length, common ones are banned and repeated failures lock the account.",
            topic="password_policy",
            scope="Microsoft Entra ID's defaults (8 characters, a banned password list and smart lockout) "
            "aren't read from the tenant. Add other systems that take passwords.",
        ),
        Control(
            "accounts",
            "",
            "Access removed when people leave",
            "Leavers' accounts are disabled promptly and inactive accounts removed.",
            rules=("stale_users", "licensed_disabled"),
            topic="leavers",
            report="inactive",
            scope="Judged from sign-in activity in Microsoft Entra ID. Describe the leavers process in the answer.",
        ),
        Control(
            "guest_review",
            "",
            "Guest access reviewed",
            "Guests and external users are reviewed and pending invitations cleared.",
            rules=("pending_guests", "guest_access_level"),
            topic="guest_review",
            report="guests",
            scope="Judged from guest accounts and the external collaboration settings.",
        ),
    ),
)

DATA = Theme(
    "data",
    "Data protection",
    "Where data lives, encryption at rest and in transit, and sharing under control.",
    (
        Control(
            "data_location",
            "",
            "Where data is stored",
            "The region the tenant's data is held in.",
            topic="data_location",
            scope="Microsoft 365 reports the tenant's data location; add any other systems.",
        ),
        Control(
            "encryption",
            "",
            "Encrypted at rest and in transit",
            "Data is encrypted at rest and in transit.",
            topic="encryption",
            scope="Microsoft 365 encrypts stored data and connections; add other systems.",
        ),
        Control(
            "sharing",
            "",
            "External sharing controlled",
            "Data can't be shared with unauthorised people: sharing settings, anyone links and guest invitations.",
            rules=("sharepoint_sharing", "anyone_links", "guest_invites"),
            topic="external_sharing",
            report="shared-files",
            scope="Judged from SharePoint and OneDrive sharing settings and links.",
        ),
        Control(
            "devices_encrypted",
            "",
            "Portable devices encrypted",
            "Laptops and other portable devices are encrypted.",
            rules=("device_encryption",),
            topic="devices_encrypted",
            report="devices",
            scope="Judged for devices managed by Intune.",
        ),
    ),
)

S_EMAIL = Theme(
    "email",
    "Email security",
    "Mail filtered, domains protected against spoofing, no forwarding outside.",
    (
        Control(
            "email_filtering",
            "",
            "Mail filtered for phishing and malware",
            "Incoming email is filtered for phishing and malware.",
            topic="email_filtering",
            scope="Microsoft 365 shows which mail gateway the domains route through, not its settings.",
        ),
        Control(
            "email_auth",
            "",
            "SPF, DKIM and DMARC",
            "Every email domain publishes SPF, signs with DKIM and has a DMARC policy.",
            rules=("spf_record", "dkim_enabled", "dmarc_policy"),
            topic="email_auth",
            report="domains",
            scope="Judged from public DNS and Exchange Online for the tenant's domains.",
        ),
        Control(
            "forwarding",
            "",
            "No automatic forwarding outside",
            "Automatic forwarding of email outside the organisation is blocked.",
            rules=("auto_forward_policy", "external_forwarding", "suspicious_inbox_rules"),
            topic="forwarding",
            report="inbox-rules",
            scope="Judged from the Exchange Online outbound spam policy, mailbox forwarding and inbox rules.",
        ),
    ),
)

S_DEVICES = Theme(
    "devices",
    "Devices",
    "Managed to a baseline, supported, patched quickly and protected by anti-malware.",
    (
        Control(
            "devices_managed",
            "",
            "Devices managed to a baseline",
            "Devices are centrally managed and must meet a security baseline.",
            rules=("intune_enrolled", "device_compliance"),
            topic="devices_managed",
            report="devices",
            figures=figures_devices,
            scope="Judged from Intune enrolment and compliance for devices joined to Entra ID.",
        ),
        Control(
            "devices_patched",
            "",
            "Supported operating systems",
            "Every operating system is still supported by its vendor.",
            rules=("supported_windows",),
            topic="devices_patched",
            report="devices",
            scope="Judged for Windows computers known to Intune or Entra ID.",
        ),
        Control(
            "patching_14_days",
            "",
            "Security updates applied quickly",
            "Security updates are applied within an agreed time, 14 days for critical ones.",
            topic="patching_14_days",
            support=("device_checkin",),
            scope="Intune shows when each device last checked in, not when updates were installed.",
        ),
        Control(
            "anti_malware",
            "",
            "Anti-malware on every device",
            "Anti-malware software is installed and kept up to date on all devices.",
            topic="anti_malware",
            support=("device_compliance",),
            scope="Office Sentry doesn't read Microsoft Defender or anti-malware settings.",
        ),
    ),
)

S_MONITORING = Theme(
    "monitoring",
    "Logging and monitoring",
    "Security events logged, suspicious activity watched, vulnerabilities looked for.",
    (
        Control(
            "logging",
            "",
            "Security events logged",
            "Security events are logged and kept.",
            rules=("mailbox_auditing",),
            topic="audit_logs",
            report="tenant-settings",
            scope="Judged from Exchange Online mailbox auditing and the unified audit log.",
        ),
        Control(
            "risk_detection",
            "",
            "Suspicious activity monitored",
            "Suspicious sign-ins and users are detected and responded to.",
            rules=("risky_users", "risky_sign_ins"),
            topic="risk_detection",
            report="risky-users",
            scope="Judged from Entra ID Protection (needs Entra ID P2 for the full detections).",
        ),
        Control(
            "vulnerability_scanning",
            "",
            "Vulnerability scanning or penetration testing",
            "Vulnerability scans or penetration tests are carried out and findings fixed.",
            topic="vulnerability_scanning",
            scope=OUTSIDE,
        ),
    ),
)

S_RECOVERY = Theme(
    "recovery",
    "Incidents, backups and continuity",
    "A plan for incidents (and telling the client), tested backups, and continuity plans.",
    (
        Control(
            "incident_response",
            "",
            "Incident response and notification",
            "An incident response plan exists and clients are told about incidents affecting their data.",
            topic="incident_response",
            scope=OUTSIDE,
        ),
        Control(
            "backups",
            "",
            "Backups and tested restores",
            "Data is backed up and restores are tested.",
            topic="backups",
            scope=OUTSIDE,
        ),
        Control(
            "bcp_dr",
            "",
            "Continuity and recovery plans",
            "Business continuity and disaster recovery plans exist.",
            topic="bcp_dr",
            scope=OUTSIDE,
        ),
    ),
)

THIRD_PARTIES = Theme(
    "third_parties",
    "Third parties, people and premises",
    "Apps and sub-processors under control, staff trained and vetted, premises secured, and a firewall "
    "with MFA on remote access.",
    (
        Control(
            "apps",
            "",
            "Third-party apps controlled",
            "Users can't consent to third-party apps on their own, and apps with risky permissions are known.",
            rules=("user_consent", "risky_app_permissions"),
            topic="third_party_apps",
            report="apps",
            scope="Judged from Entra ID consent settings and app permissions.",
        ),
        Control(
            "cloud_services",
            "",
            "Cloud services and sub-processors",
            "The cloud services and sub-processors that hold the client's data.",
            topic="cloud_services",
            scope="Microsoft 365 lists the apps connected to its sign-in; add the rest.",
        ),
        Control(
            "training",
            "",
            "Security awareness training",
            "Staff receive security awareness training.",
            topic="training",
            scope=OUTSIDE,
        ),
        Control(
            "staff_vetting",
            "",
            "Staff vetted before access",
            "Staff are vetted before they're given access.",
            topic="staff_vetting",
            scope=OUTSIDE,
        ),
        Control(
            "physical_security",
            "",
            "Premises and equipment secured",
            "Premises and equipment are physically secured.",
            topic="physical_security",
            scope=OUTSIDE,
        ),
        Control(
            "firewall_boundary",
            "",
            "Network protected by firewalls",
            "The network is protected by firewalls.",
            topic="firewall_boundary",
            scope=OUTSIDE,
        ),
        Control(
            "remote_access",
            "",
            "MFA on remote access",
            "Remote access is protected by MFA.",
            topic="remote_access",
            scope=OUTSIDE,
        ),
    ),
)

SUPPLIER = register(
    Scheme(
        "supplier",
        "Supplier security questionnaire",
        "Supplier questionnaire",
        "What clients ask in supplier and vendor due-diligence questionnaires, answered for this client.",
        "forms",
        "Due-diligence questionnaires are read by a customer's security team, so the answers should be "
        "specific and true. A control missing for the whole organisation is a Fail and a gap on a few accounts "
        "is Partly: say what's in place and what's planned. Use the text for the matching question on the "
        "client's form.",
        (GOVERNANCE, S_ACCESS, DATA, S_EMAIL, S_DEVICES, S_MONITORING, S_RECOVERY, THIRD_PARTIES),
        "Supplier and vendor due-diligence questionnaires as commonly worded in 2026",
        "Questions drawn from the supplier security questionnaires UK organisations send their suppliers, in "
        "their typical wording; no organisation's form is copied.",
        "",
        strict=False,
        questions="supplier",
        notes=(
            "Scope: the client's customer asks about the client's whole business. A Pass here covers Microsoft "
            "365; the answer should say what covers the rest.",
        ),
        renewal="",
    )
)
