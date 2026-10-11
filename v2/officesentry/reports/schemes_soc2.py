"""SOC 2: the Trust Services Criteria a Microsoft 365 tenant gives evidence for.

A SOC 2 report is an auditor's opinion on a service organisation's controls against the AICPA Trust
Services Criteria. The common criteria (CC1 to CC9) cover the control environment, risk, monitoring,
logical access, system operations and change; Availability adds backups and recovery. An MSP's client is
usually asked for evidence of the access, operations and change criteria, which is where Microsoft 365
speaks. Criteria identifiers are the AICPA's; the criteria are paraphrased.
"""

from __future__ import annotations

from . import evidence_topics  # noqa: F401  (the topics the controls name must be registered first)
from .schemes import Audit, Control, Scheme, Theme, figures_admins, figures_devices, figures_mfa_users, register

OUTSIDE = "All of this. Answer from the organisation's own records."

ENVIRONMENT = Theme(
    "environment",
    "Control environment, risk and communication (CC1 to CC5)",
    "Oversight and accountability, competent and vetted people, policies, and risk assessment.",
    (
        Control(
            "cc1_3",
            "CC1.3",
            "Structures, reporting lines and responsibilities",
            "Management sets structures, reporting lines and responsibilities for security.",
            topic="security_responsibility",
            scope=OUTSIDE,
        ),
        Control(
            "cc1_4",
            "CC1.4",
            "Competent, vetted people",
            "People are checked before they join and trained for their responsibilities.",
            topic="staff_vetting",
            scope=OUTSIDE,
        ),
        Control(
            "cc2_2",
            "CC2.2",
            "Security responsibilities communicated",
            "Security responsibilities are communicated to staff, including through training.",
            topic="training",
            scope=OUTSIDE,
        ),
        Control(
            "cc3_2",
            "CC3.2",
            "Risks identified and analysed",
            "Risks to the service's objectives are identified and analysed.",
            topic="risk_assessment",
            scope=OUTSIDE,
        ),
        Control(
            "cc5_3",
            "CC5.3",
            "Policies and procedures",
            "Control activities are set out in policies and put into practice through procedures.",
            topic="policies",
            scope=OUTSIDE,
        ),
    ),
)

ACCESS = Theme(
    "access",
    "Logical and physical access (CC6)",
    "Access is authenticated and authorised, given and removed by process, data movement is restricted and "
    "devices are protected.",
    (
        Control(
            "cc6_1",
            "CC6.1",
            "Logical access security",
            "Access to systems and data is protected by authentication: multi-factor for everyone, legacy protocols "
            "blocked, and data encrypted.",
            rules=("mfa_enforced", "admins_ca_mfa", "legacy_auth_blocked", "smtp_auth"),
            topic="mfa_remote",
            support=("admin_phishing_resistant", "device_encryption"),
            report="mfa",
            figures=figures_mfa_users,
            scope="Judged for Microsoft 365 sign-ins. Add the service's own systems.",
        ),
        Control(
            "cc6_2",
            "CC6.2",
            "Access registered and authorised",
            "New users and guests are registered and authorised before they're given access; old invitations go.",
            rules=("guest_invites", "pending_guests"),
            topic="account_approval",
            report="guests",
            scope="Microsoft 365 shows who can invite guests; describe how staff accounts are approved.",
        ),
        Control(
            "cc6_3",
            "CC6.3",
            "Access based on role, removed when not needed",
            "Access follows least privilege and is removed when people leave or change role; admin rights are few.",
            rules=("stale_users", "licensed_disabled", "global_admin_count", "guest_admins"),
            topic="least_privilege",
            report="inactive",
            figures=figures_admins,
            scope="Judged from Entra ID sign-in activity and admin roles.",
        ),
        Control(
            "cc6_4",
            "CC6.4",
            "Physical access restricted",
            "Physical access to facilities and equipment is restricted to authorised people.",
            topic="physical_security",
            scope=OUTSIDE,
        ),
        Control(
            "cc6_5",
            "CC6.5",
            "Data removed from disposed assets",
            "Data is removed from equipment before it's disposed of.",
            topic="secure_disposal",
            scope=OUTSIDE,
        ),
        Control(
            "cc6_6",
            "CC6.6",
            "Protection from outside threats",
            "Access from outside is restricted: firewalls at the boundary and no open sharing.",
            rules=("sharepoint_sharing", "anyone_links"),
            topic="firewall_boundary",
            report="shared-files",
            scope="Microsoft 365 shows its sharing; describe the network boundary.",
        ),
        Control(
            "cc6_7",
            "CC6.7",
            "Transmission and movement of data restricted",
            "Data moving outside is restricted and protected: no unnoticed forwarding, domains can't be spoofed.",
            rules=("auto_forward_policy", "external_forwarding", "dmarc_policy"),
            topic="forwarding",
            support=("mta_sts", "spf_record", "dkim_enabled"),
            report="inbox-rules",
            scope="Judged from Exchange Online forwarding and public DNS.",
        ),
        Control(
            "cc6_8",
            "CC6.8",
            "Malicious software prevented or detected",
            "Unauthorised or malicious software is prevented or detected on managed devices.",
            rules=("device_compliance", "intune_enrolled"),
            topic="anti_malware",
            report="devices",
            figures=figures_devices,
            scope="Intune shows devices are managed and compliant; describe the anti-malware product.",
        ),
    ),
)

OPERATIONS = Theme(
    "operations",
    "System operations and change (CC7 and CC8)",
    "Vulnerabilities and anomalies are detected, events are evaluated, incidents responded to and recovered "
    "from, and changes are controlled.",
    (
        Control(
            "cc7_1",
            "CC7.1",
            "Vulnerabilities and configuration changes detected",
            "Vulnerabilities and unexpected configuration changes are found, including systems out of support.",
            rules=("supported_windows", "domain_federation_changed"),
            topic="vulnerability_scanning",
            report="devices",
            scope="Judged from Intune and Entra ID domain settings. Describe vulnerability scanning.",
        ),
        Control(
            "cc7_2",
            "CC7.2",
            "System components monitored for anomalies",
            "Sign-ins, accounts and mailboxes are monitored for anomalies, and activity is logged.",
            rules=("risky_users", "risky_sign_ins", "suspicious_inbox_rules", "mailbox_auditing"),
            topic="risk_detection",
            report="risky-users",
            scope="Judged from Entra ID Protection, inbox rules and Exchange auditing.",
        ),
        Control(
            "cc7_3",
            "CC7.3",
            "Security events evaluated",
            "Security events are evaluated to decide whether they're incidents.",
            topic="log_review",
            scope=OUTSIDE,
        ),
        Control(
            "cc7_4",
            "CC7.4",
            "Incidents responded to",
            "Security incidents are contained, fixed and communicated by a defined programme.",
            topic="incident_response",
            scope=OUTSIDE,
        ),
        Control(
            "cc7_5",
            "CC7.5",
            "Recovery from incidents",
            "Activities to recover from incidents are identified and carried out.",
            topic="bcp_dr",
            scope=OUTSIDE,
        ),
        Control(
            "cc8_1",
            "CC8.1",
            "Change management",
            "Changes to infrastructure, data and software are authorised, tested and approved; tenant settings that "
            "let users make their own changes are locked down.",
            rules=("app_registration", "tenant_creation", "group_creation"),
            topic="change_management",
            report="tenant-settings",
            scope="Microsoft 365 shows the tenant settings; describe the change process.",
        ),
    ),
)

VENDORS = Theme(
    "vendors",
    "Risk mitigation and availability (CC9 and A1)",
    "Vendors and partners are managed, and data is backed up and recoverable.",
    (
        Control(
            "cc9_2",
            "CC9.2",
            "Vendor and partner risk",
            "Risks from vendors and business partners are assessed and managed, including apps connected to the "
            "tenant.",
            rules=("user_consent", "risky_app_permissions"),
            topic="supplier_security",
            report="apps",
            scope="Microsoft 365 shows which apps reach its data; describe the vendor reviews.",
        ),
        Control(
            "a1_2",
            "A1.2",
            "Backups and recovery infrastructure",
            "Data is backed up and the recovery infrastructure is in place and monitored.",
            topic="backups",
            scope="Microsoft 365 doesn't back itself up for this purpose; describe the backup of Microsoft 365 too.",
        ),
    ),
)

SOC2 = register(
    Scheme(
        "soc2",
        "SOC 2 Trust Services Criteria",
        "SOC 2",
        "AICPA",
        "international",
        "The Trust Services Criteria Microsoft 365 gives evidence for, judged from the data, and text for the rest. "
        "Use it to prepare for a SOC 2 audit or answer a customer who holds one.",
        (ENVIRONMENT, ACCESS, OPERATIONS, VENDORS),
        "AICPA 2017 Trust Services Criteria (revised points of focus, 2022): Security and Availability",
        "Criteria identifiers are the AICPA's; criteria are paraphrased. This pack covers the criteria Microsoft 365 "
        "speaks to, not every point of focus.",
        "https://www.aicpa-cima.com/resources/landing/system-and-organization-controls-soc-suite-of-services",
        audit=(
            Audit(
                "Type 2 testing over the period",
                "The auditor tests that each control worked throughout the period (often 6 or 12 months), sampling "
                "access changes, leavers, alerts and changes.",
                "Microsoft 365 shows the access and monitoring controls on this page, and the What changed view "
                "shows how each one stood at the start of the period.",
                ("cc6_1", "cc6_3", "cc7_2", "cc8_1"),
            ),
        ),
        order=40,
        notes=(
            "A Pass is evidence for the Microsoft 365 part of a criterion; the auditor still tests the service's own "
            "systems and processes.",
        ),
        renewal="12 months",
    )
)
