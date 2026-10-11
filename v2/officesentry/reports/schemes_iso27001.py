"""ISO/IEC 27001:2022 Annex A: the controls Microsoft 365 evidences, and the ones auditors ask an MSP about.

Annex A lists 93 controls in four themes (organisational, people, physical, technological). An organisation
picks the ones that apply in its Statement of Applicability, and an auditor looks for evidence that each is
in place. This pack covers the Annex A controls Microsoft 365 data speaks to, judged from the checks, plus the
ones an MSP is usually asked to evidence, as text to complete. It is not the whole Statement of Applicability,
and the management system clauses (4 to 10) are the organisation's own. Control numbers are ISO's; titles and
requirements are paraphrased, never ISO's text.

Not ``strict``: an auditor raises a minor nonconformity for a gap on a few accounts and a major one for a
control that's missing, so a gap is Partly unless it covers the whole organisation.
"""

from __future__ import annotations

from . import evidence_topics  # noqa: F401  (the topics the controls name must be registered first)
from .schemes import (
    Audit,
    Control,
    Scheme,
    Theme,
    figures_admins,
    figures_devices,
    figures_mfa_users,
    register,
)

OUTSIDE = "All of this. Answer from the organisation's own records."

ORGANISATIONAL = Theme(
    "organisational",
    "Organisational controls (5)",
    "Policies, roles, assets, access rules, suppliers and cloud services, incidents and continuity.",
    (
        Control(
            "5_1",
            "5.1",
            "Information security policies",
            "A security policy and topic-specific policies are defined, approved by management, published, "
            "acknowledged by staff and reviewed.",
            topic="policies",
            scope=OUTSIDE,
        ),
        Control(
            "5_2",
            "5.2",
            "Security roles and responsibilities",
            "Responsibilities for information security are defined and given to named people.",
            topic="security_responsibility",
            scope=OUTSIDE,
        ),
        Control(
            "5_9",
            "5.9",
            "Inventory of information and assets",
            "Information and the assets that hold it are listed, with an owner for each.",
            topic="asset_register",
            support=("stale_devices", "intune_enrolled"),
            report="devices",
            figures=figures_devices,
            scope="Microsoft 365 lists the devices Intune and Entra ID know; the inventory covers everything else.",
        ),
        Control(
            "5_14",
            "5.14",
            "Information transfer",
            "Rules protect information sent by email and other means: domains can't be spoofed and mail travels "
            "encrypted.",
            rules=("spf_record", "dkim_enabled", "dmarc_policy"),
            topic="email_auth",
            support=("mta_sts", "tls_rpt"),
            report="domains",
            scope="Judged from public DNS and Exchange Online for the tenant's domains.",
        ),
        Control(
            "5_15",
            "5.15",
            "Access control",
            "Rules for who can reach which information are set by business need, including what guests can see.",
            rules=("guest_access_level", "guest_invites", "sharepoint_sharing"),
            topic="least_privilege",
            report="guests",
            scope="Judged from Entra ID guest settings and SharePoint sharing. Describe how staff access is decided.",
        ),
        Control(
            "5_16",
            "5.16",
            "Identity management",
            "Every identity belongs to one person and is removed when it's no longer needed; shared accounts can't "
            "sign in.",
            rules=("stale_users", "licensed_disabled", "shared_mailbox_signin"),
            topic="leavers",
            report="inactive",
            scope="Judged from Entra ID accounts, sign-in activity and shared mailboxes.",
        ),
        Control(
            "5_17",
            "5.17",
            "Authentication information",
            "Passwords and other sign-in secrets are protected: everyone has a second factor, and weak ones are "
            "avoided.",
            rules=("users_mfa_registered", "phone_only_mfa"),
            topic="password_policy",
            report="auth-methods",
            scope="Judged from Entra ID authentication methods. Add other systems that use passwords.",
        ),
        Control(
            "5_18",
            "5.18",
            "Access rights reviewed",
            "Access rights are given, reviewed and removed through a defined process, including guests.",
            rules=("pending_guests",),
            topic="admin_review",
            support=("ownerless_groups", "group_expiration"),
            report="guests",
            scope="Microsoft 365 shows guests and group owners; describe the review and when it last happened.",
        ),
        Control(
            "5_19",
            "5.19",
            "Security in supplier relationships",
            "Security risks from suppliers are managed, with requirements agreed before they get access.",
            topic="supplier_security",
            scope=OUTSIDE,
        ),
        Control(
            "5_20",
            "5.20",
            "Security in supplier agreements",
            "Contracts with suppliers set out their security and data protection obligations.",
            topic="processor_contracts",
            scope=OUTSIDE,
        ),
        Control(
            "5_23",
            "5.23",
            "Security for use of cloud services",
            "Cloud services are taken on, used and left under the organisation's control: people can't connect "
            "apps to company data on their own, and app access is known.",
            rules=("user_consent", "risky_app_permissions", "app_credentials"),
            topic="third_party_apps",
            support=("admin_consent_workflow",),
            report="apps",
            scope="Judged from Entra ID consent settings, app permissions and app credentials.",
        ),
        Control(
            "5_24",
            "5.24",
            "Incident management planning",
            "Roles and procedures for handling security incidents are planned and documented.",
            topic="incident_response",
            scope=OUTSIDE,
        ),
        Control(
            "5_26",
            "5.26",
            "Response to incidents",
            "Incidents are responded to by the documented procedure, such as containing a compromised account.",
            topic="compromise_process",
            scope=OUTSIDE,
        ),
        Control(
            "5_29",
            "5.29",
            "Security during disruption",
            "Security is kept up during disruption, with continuity plans that are tested.",
            topic="bcp_dr",
            scope=OUTSIDE,
        ),
        Control(
            "5_34",
            "5.34",
            "Privacy and protection of personal data",
            "Legal requirements for personal data (UK GDPR in the UK) are identified and met.",
            topic="ropa",
            scope=OUTSIDE,
        ),
    ),
)

PEOPLE = Theme(
    "people",
    "People controls (6)",
    "Screening, awareness and training, leavers, and remote working.",
    (
        Control(
            "6_1",
            "6.1",
            "Screening",
            "People are checked before they join, in proportion to the information they'll reach.",
            topic="staff_vetting",
            scope=OUTSIDE,
        ),
        Control(
            "6_3",
            "6.3",
            "Awareness, education and training",
            "Staff get security awareness training and regular updates relevant to their job.",
            topic="training",
            scope=OUTSIDE,
        ),
        Control(
            "6_5",
            "6.5",
            "Responsibilities after leaving",
            "Access ends when people leave or change role, and duties that continue are communicated.",
            rules=("stale_users", "licensed_disabled"),
            topic="leavers",
            report="inactive",
            scope="Judged from sign-in activity in Entra ID. Describe the leavers process.",
        ),
        Control(
            "6_7",
            "6.7",
            "Remote working",
            "People working away from the office reach company data securely.",
            rules=("mfa_enforced", "legacy_auth_blocked"),
            topic="remote_workers",
            report="mfa",
            scope="Judged for Microsoft 365 sign-ins. Add VPNs and remote desktop.",
        ),
        Control(
            "6_8",
            "6.8",
            "Reporting security events",
            "Staff know how to report suspected security events quickly.",
            topic="breach_reporting",
            scope=OUTSIDE,
        ),
    ),
)

PHYSICAL = Theme(
    "physical",
    "Physical controls (7)",
    "Premises, equipment away from the office, storage media and disposal.",
    (
        Control(
            "7_1",
            "7.1 to 7.4",
            "Secure premises",
            "Offices and secure areas have perimeters, entry controls and monitoring.",
            topic="physical_security",
            scope=OUTSIDE,
        ),
        Control(
            "7_9",
            "7.9",
            "Assets away from the premises",
            "Laptops and other equipment used away from the office are protected, including by encryption.",
            rules=("device_encryption",),
            topic="devices_encrypted",
            report="devices",
            scope="Judged from Intune for company computers.",
        ),
        Control(
            "7_10",
            "7.10",
            "Storage media",
            "Removable storage is controlled from first use to disposal.",
            topic="removable_media",
            scope=OUTSIDE,
        ),
        Control(
            "7_14",
            "7.14",
            "Secure disposal or re-use",
            "Data is securely removed before equipment is reused or disposed of.",
            topic="secure_disposal",
            scope=OUTSIDE,
        ),
    ),
)

TECHNOLOGICAL = Theme(
    "technological",
    "Technological controls (8)",
    "Endpoints, privileged access, authentication, malware, vulnerabilities, configuration, leakage, backup, "
    "logging, monitoring, networks, web filtering, cryptography and change.",
    (
        Control(
            "8_1",
            "8.1",
            "User endpoint devices",
            "Laptops, desktops and phones that reach company data are managed and meet policy.",
            rules=("intune_enrolled", "device_compliance", "device_checkin"),
            topic="devices_managed",
            report="devices",
            figures=figures_devices,
            scope="Judged from Intune and Entra ID.",
        ),
        Control(
            "8_2",
            "8.2",
            "Privileged access rights",
            "Administrator rights are few, given to named internal people, and separate from everyday accounts.",
            rules=("global_admin_count", "guest_admins", "service_principal_admins", "cloud_only_admins"),
            topic="admin_count",
            support=("admin_mfa",),
            report="admins",
            figures=figures_admins,
            scope="Judged from Microsoft 365 and Entra ID admin roles. Add administrators of other systems.",
        ),
        Control(
            "8_3",
            "8.3",
            "Information access restriction",
            "Access to files is restricted as the access rules say: nothing open to anyone with the link.",
            rules=("anyone_links", "sharepoint_sharing"),
            topic="external_sharing",
            report="shared-files",
            scope="Judged from SharePoint and OneDrive sharing settings and links.",
        ),
        Control(
            "8_5",
            "8.5",
            "Secure authentication",
            "Sign-in is protected by multi-factor authentication, for administrators especially, and protocols "
            "that can't use it are blocked.",
            rules=("mfa_enforced", "admins_ca_mfa", "legacy_auth_blocked", "smtp_auth"),
            topic="mfa_remote",
            support=("admin_phishing_resistant",),
            report="mfa",
            figures=figures_mfa_users,
            scope="Judged from Conditional Access, Security Defaults and Exchange Online for Microsoft 365.",
        ),
        Control(
            "8_7",
            "8.7",
            "Protection against malware",
            "Malware protection runs on every device, and mail is filtered for malicious content.",
            topic="anti_malware",
            support=("device_compliance",),
            scope=OUTSIDE,
        ),
        Control(
            "8_8",
            "8.8",
            "Management of technical vulnerabilities",
            "Vulnerabilities are found and fixed in time, and systems that no longer get updates are replaced.",
            rules=("supported_windows",),
            topic="devices_patched",
            report="devices",
            figures=figures_devices,
            scope="Judged from the Windows versions Intune reports. Add servers and network devices.",
        ),
        Control(
            "8_9",
            "8.9",
            "Configuration management",
            "Secure settings are defined and kept: ordinary users can't create tenants, register apps or join by "
            "verifying an email address.",
            rules=("tenant_creation", "app_registration", "email_verified_join", "group_creation"),
            topic="change_management",
            report="tenant-settings",
            scope="Judged from Entra ID tenant settings. Describe how settings are recorded and changed.",
        ),
        Control(
            "8_10",
            "8.10",
            "Information deletion",
            "Information is deleted when it's no longer needed.",
            topic="data_retention",
            scope=OUTSIDE,
        ),
        Control(
            "8_12",
            "8.12",
            "Data leakage prevention",
            "Mail can't be forwarded outside unnoticed, and inbox rules that hide or forward mail are caught.",
            rules=("auto_forward_policy", "external_forwarding", "suspicious_inbox_rules"),
            topic="forwarding",
            report="inbox-rules",
            scope="Judged from the Exchange Online outbound spam policy, mailbox forwarding and inbox rules.",
        ),
        Control(
            "8_13",
            "8.13",
            "Information backup",
            "Backups are taken and restores tested, as the backup policy says.",
            topic="backups",
            scope="Microsoft 365 doesn't back itself up for this purpose; describe the backup of Microsoft 365 too.",
        ),
        Control(
            "8_15",
            "8.15",
            "Logging",
            "Logs of activity and security events are produced, kept and protected.",
            rules=("mailbox_auditing",),
            topic="audit_logs",
            report="tenant-settings",
            scope="Judged from Exchange Online mailbox auditing and the unified audit log.",
        ),
        Control(
            "8_16",
            "8.16",
            "Monitoring activities",
            "Networks, systems and sign-ins are watched for unusual behaviour, and it's acted on.",
            rules=("risky_users", "risky_sign_ins"),
            topic="risk_detection",
            support=("domain_federation_changed",),
            report="risky-users",
            scope="Judged from Entra ID Protection (needs Entra ID P2 for the full detections).",
        ),
        Control(
            "8_20",
            "8.20",
            "Network security",
            "Networks are protected by firewalls and managed.",
            topic="firewall_boundary",
            scope=OUTSIDE,
        ),
        Control(
            "8_23",
            "8.23",
            "Web filtering",
            "Access to malicious websites is blocked.",
            topic="web_filtering",
            scope=OUTSIDE,
        ),
        Control(
            "8_24",
            "8.24",
            "Use of cryptography",
            "Encryption protects data at rest and in transit, by an agreed approach.",
            topic="encryption",
            support=("device_encryption", "mta_sts"),
            scope="Microsoft encrypts Microsoft 365 data itself; laptops are judged from Intune.",
        ),
        Control(
            "8_32",
            "8.32",
            "Change management",
            "Changes to systems are planned, tested and approved.",
            topic="change_management",
            scope=OUTSIDE,
        ),
    ),
)

ISO_27001 = register(
    Scheme(
        "iso-27001",
        "ISO/IEC 27001 Annex A",
        "ISO 27001",
        "ISO and IEC",
        "international",
        "The Annex A controls Microsoft 365 data speaks to, judged for the Statement of Applicability and the audit, "
        "and the others an MSP is asked to evidence, as text to complete.",
        (ORGANISATIONAL, PEOPLE, PHYSICAL, TECHNOLOGICAL),
        "ISO/IEC 27001:2022 with Amendment 1:2024, Annex A",
        "Control numbers are ISO's; titles and requirements are paraphrased. This pack is part of the Statement of "
        "Applicability, not all of it: the other Annex A controls and the management system clauses 4 to 10 are the "
        "organisation's own.",
        "https://www.iso.org/standard/27001",
        audit=(
            Audit(
                "Stage 2 certification and surveillance audits",
                "The auditor samples Annex A controls from the Statement of Applicability and asks for evidence that "
                "each works in practice: records, settings and interviews.",
                "Microsoft 365 can show the technical evidence the day it's asked for: authentication, privileged "
                "access, endpoints, logging and leakage controls on this page, with the rows behind them, and what "
                "changed since the last audit.",
                ("8_5", "8_2", "8_1", "8_15", "8_12"),
            ),
        ),
        notes=(
            "Use the controls marked Pass as evidence in the Statement of Applicability, with this pack's date. A "
            "Partly is a likely minor nonconformity; a Fail is a likely major one.",
            "Scope: the ISMS scope may be wider than Microsoft 365. A Pass here covers Microsoft 365; say what covers "
            "the rest.",
        ),
        renewal="12 months",
    )
)
