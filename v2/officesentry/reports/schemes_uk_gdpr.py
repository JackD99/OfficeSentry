"""UK GDPR security and accountability, judged by the ICO's security outcomes where Microsoft 365 can speak to them.

Article 32 asks for security appropriate to the risk, and Article 5(1)(f) for integrity and confidentiality.
The ICO, with the NCSC, sets out what "appropriate" looks like as security outcomes under four aims: manage
security risk, protect personal data against cyber-attack, detect security events, and minimise the impact.
This pack follows those outcomes, then adds the accountability records an organisation is asked for after a
breach or by a client (the record of processing, processor contracts, the privacy notice, rights requests,
impact assessments, retention and the ICO fee). The law and the outcomes are paraphrased, never copied.

Not ``strict``: the ICO judges what was appropriate to the risk, so a gap on a few accounts is Partly.
"""

from __future__ import annotations

from . import evidence_topics  # noqa: F401  (the topics the controls name must be registered first)
from .schemes import Control, Scheme, Theme, figures_admins, figures_devices, figures_mfa_users, register

OUTSIDE = "All of this. Answer from the organisation's own records."

MANAGE = Theme(
    "manage",
    "A. Manage security risk",
    "Someone is accountable, risks to personal data are assessed, the data and systems are known, and processors "
    "are under contract.",
    (
        Control(
            "a1_governance",
            "A.1",
            "Governance: accountable people and policies",
            "Senior management is accountable for the security of personal data, with named roles and policies.",
            topic="security_responsibility",
            scope=OUTSIDE,
        ),
        Control(
            "a1_dpo",
            "A.1",
            "Data protection officer or lead",
            "A Data Protection Officer is appointed where the law requires one; otherwise a named person leads.",
            topic="dpo",
            scope=OUTSIDE,
        ),
        Control(
            "a2_risk",
            "A.2",
            "Risk management",
            "Risks to personal data are assessed, recorded and treated, and the assessment is kept current.",
            topic="risk_assessment",
            scope=OUTSIDE,
        ),
        Control(
            "a3_assets",
            "A.3",
            "Asset management",
            "The systems and devices that hold personal data are known, with owners.",
            topic="asset_register",
            support=("stale_devices", "intune_enrolled"),
            report="devices",
            figures=figures_devices,
            scope="Microsoft 365 lists the devices Intune and Entra ID know; the register covers everything else.",
        ),
        Control(
            "a4_processors",
            "A.4",
            "Processors and the supply chain",
            "Processors are chosen for their security and bound by contract (Article 28), and the cloud apps that "
            "reach the data are controlled.",
            rules=("user_consent", "risky_app_permissions"),
            topic="processor_contracts",
            report="apps",
            scope="Microsoft 365 shows which apps can reach its data; the contracts are the organisation's own.",
        ),
        Control(
            "a4_location",
            "A.4",
            "Where the data is held",
            "The organisation knows where personal data is stored, and transfers outside the UK have a lawful "
            "safeguard.",
            topic="data_location",
            scope="Judged from the tenant's country in Microsoft 365. Add other services that hold personal data.",
        ),
    ),
)

PROTECT = Theme(
    "protect",
    "B. Protect personal data against cyber-attack",
    "Policies are followed, access is by named accounts with MFA, data is encrypted and not shared openly, systems "
    "are updated and managed, and staff are trained.",
    (
        Control(
            "b1_policies",
            "B.1",
            "Service protection policies and processes",
            "Policies and processes say how personal data and the systems that hold it are protected.",
            topic="policies",
            scope=OUTSIDE,
        ),
        Control(
            "b2_mfa",
            "B.2",
            "Identity and access: multi-factor authentication",
            "Everyone who can reach personal data signs in with multi-factor authentication, and protocols that "
            "can't use it are blocked.",
            rules=("mfa_enforced", "admins_ca_mfa", "legacy_auth_blocked"),
            topic="mfa_remote",
            support=("users_mfa_registered", "phone_only_mfa"),
            report="mfa",
            figures=figures_mfa_users,
            scope="Judged for Microsoft 365 sign-ins. Add other systems that hold personal data.",
        ),
        Control(
            "b2_admins",
            "B.2",
            "Identity and access: administrators",
            "Administrator access is limited to named people who need it.",
            rules=("global_admin_count", "guest_admins", "service_principal_admins"),
            topic="admin_count",
            report="admins",
            figures=figures_admins,
            scope="Judged from Microsoft 365 and Entra ID admin roles.",
        ),
        Control(
            "b2_accounts",
            "B.2",
            "Identity and access: leavers and shared accounts",
            "Access ends when people leave, and every sign-in belongs to a person.",
            rules=("stale_users", "licensed_disabled", "shared_mailbox_signin"),
            topic="leavers",
            report="inactive",
            scope="Judged from Entra ID accounts, sign-in activity and shared mailboxes.",
        ),
        Control(
            "b3_sharing",
            "B.3",
            "Data security: sharing",
            "Personal data isn't open to anyone with a link, and guests are controlled.",
            rules=("anyone_links", "sharepoint_sharing", "guest_invites"),
            topic="external_sharing",
            report="shared-files",
            scope="Judged from SharePoint and OneDrive sharing settings and links.",
        ),
        Control(
            "b3_email",
            "B.3",
            "Data security: email leaving the organisation",
            "Mail isn't forwarded outside unnoticed, and the domains can't be spoofed to trick people into sending "
            "data.",
            rules=("auto_forward_policy", "external_forwarding", "dmarc_policy"),
            topic="forwarding",
            support=("spf_record", "dkim_enabled", "mta_sts"),
            report="inbox-rules",
            scope="Judged from Exchange Online forwarding settings and public DNS.",
        ),
        Control(
            "b3_encryption",
            "B.3",
            "Data security: encryption",
            "Personal data is encrypted on laptops and other devices that could be lost, and in transit.",
            rules=("device_encryption",),
            topic="encryption",
            support=("mta_sts",),
            report="devices",
            scope="Judged from Intune for company computers; Microsoft encrypts Microsoft 365 data itself.",
        ),
        Control(
            "b4_systems",
            "B.4",
            "System security: updates and device management",
            "Devices are managed and supported by their vendor, and security updates are installed promptly.",
            rules=("supported_windows", "device_compliance", "intune_enrolled"),
            topic="devices_patched",
            report="devices",
            figures=figures_devices,
            scope="Judged from Intune. Add servers and devices it doesn't manage.",
        ),
        Control(
            "b4_malware",
            "B.4",
            "System security: malware protection",
            "Every device is protected from malware.",
            topic="anti_malware",
            scope=OUTSIDE,
        ),
        Control(
            "b5_training",
            "B.5",
            "Staff awareness and training",
            "Staff are trained to handle personal data securely and to spot phishing.",
            topic="training",
            scope=OUTSIDE,
        ),
    ),
)

DETECT = Theme(
    "detect",
    "C. Detect security events",
    "Activity is logged, suspicious sign-ins and mailbox rules are caught, and someone looks.",
    (
        Control(
            "c1_logging",
            "C.1",
            "Security monitoring: logging",
            "Access to personal data and administrator actions are logged.",
            rules=("mailbox_auditing",),
            topic="audit_logs",
            report="tenant-settings",
            scope="Judged from Exchange Online mailbox auditing and the unified audit log.",
        ),
        Control(
            "c1_detection",
            "C.1",
            "Security monitoring: suspicious activity",
            "Suspicious sign-ins, risky accounts and mailbox rules that hide or forward mail are detected.",
            rules=("risky_users", "risky_sign_ins", "suspicious_inbox_rules"),
            topic="risk_detection",
            report="risky-users",
            scope="Judged from Entra ID Protection and inbox rules (Entra ID P2 for the full detections).",
        ),
        Control(
            "c1_review",
            "C.1",
            "Security monitoring: alerts reviewed",
            "Alerts and logs are reviewed by someone who can act on them.",
            topic="log_review",
            scope=OUTSIDE,
        ),
    ),
)

MINIMISE = Theme(
    "minimise",
    "D. Minimise the impact",
    "Incidents are handled to a plan, breaches go to the ICO within 72 hours when required, data can be restored, "
    "and lessons are learned.",
    (
        Control(
            "d1_response",
            "D.1",
            "Response and recovery planning",
            "There is a plan for responding to incidents, including containing a compromised account.",
            topic="incident_response",
            support=("risky_users",),
            scope=OUTSIDE,
        ),
        Control(
            "d1_breaches",
            "D.1",
            "Breach reporting within 72 hours",
            "Personal data breaches are recorded, reported to the ICO within 72 hours of becoming aware when they're "
            "reportable, and the people affected are told when the risk to them is high (Articles 33 and 34).",
            topic="breach_reporting",
            scope=OUTSIDE,
        ),
        Control(
            "d1_backups",
            "D.1",
            "Restoring personal data",
            "Personal data can be restored promptly after an incident, from backups that are tested.",
            topic="backups",
            scope="Microsoft 365 doesn't back itself up for this purpose; describe the backup of Microsoft 365 too.",
        ),
        Control(
            "d2_improve",
            "D.2",
            "Improvements: measures tested regularly",
            "The security measures are tested and assessed regularly (Article 32(1)(d)), for example by "
            "vulnerability scans, and lessons from incidents and tests are acted on.",
            topic="vulnerability_scanning",
            scope=OUTSIDE,
        ),
    ),
)

ACCOUNTABILITY = Theme(
    "accountability",
    "Accountability records",
    "What a client, an insurer or the ICO asks to see after a breach: the record of processing, the privacy notice, "
    "rights requests, impact assessments, retention and the ICO fee.",
    (
        Control(
            "ropa",
            "Art. 30",
            "Record of processing activities",
            "A record of the personal data held, why, on what lawful basis, who it's shared with and how long it's "
            "kept.",
            topic="ropa",
            scope=OUTSIDE,
        ),
        Control(
            "privacy_notice",
            "Art. 13 and 14",
            "Privacy notice",
            "People are told what's collected, why, how long it's kept, their rights and how to complain.",
            topic="privacy_notice",
            scope=OUTSIDE,
        ),
        Control(
            "rights",
            "Art. 15 to 22",
            "People's rights",
            "Requests to see, correct or delete personal data are answered within a month, and complaints handled.",
            topic="data_subject_rights",
            scope=OUTSIDE,
        ),
        Control(
            "dpia",
            "Art. 35",
            "Data protection impact assessments",
            "An impact assessment is done before processing likely to be high risk to people.",
            topic="dpia",
            scope=OUTSIDE,
        ),
        Control(
            "retention",
            "Art. 5(1)(e)",
            "Kept no longer than needed",
            "Personal data is kept only as long as it's needed, then deleted.",
            topic="data_retention",
            scope=OUTSIDE,
        ),
        Control(
            "ico_fee",
            "",
            "ICO data protection fee",
            "The organisation has paid the data protection fee to the ICO, unless it's exempt.",
            topic="ico_registration",
            scope=OUTSIDE,
        ),
    ),
)

UK_GDPR = register(
    Scheme(
        "uk-gdpr",
        "UK GDPR security and accountability",
        "UK GDPR",
        "ICO",
        "privacy",
        "How the client's Microsoft 365 meets the ICO's security outcomes for UK GDPR, with the accountability "
        "records a client, insurer or the ICO asks for. Use it for a client's data protection review, a DPIA or "
        "the questions after a breach.",
        (MANAGE, PROTECT, DETECT, MINIMISE, ACCOUNTABILITY),
        "UK GDPR Articles 5, 24, 28, 30, 32 to 35, and the ICO's security outcomes (A to D)",
        "The security outcomes are the ICO's, set out with the NCSC, and are under review after the Data (Use and "
        "Access) Act 2025; check the ICO's guide to data security for changes. The law and outcomes are paraphrased.",
        "https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/security/a-guide-to-data-security/",
        notes=(
            "The ICO judges what was appropriate to the risk and the state of the art, so a gap here isn't "
            "automatically a breach of the law, but it's what the ICO would ask about after an incident.",
            "Scope: personal data held outside Microsoft 365 (line-of-business systems, paper) needs the same "
            "measures. A Pass here covers Microsoft 365.",
        ),
    )
)
