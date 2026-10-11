"""IASME Cyber Assurance: the UK certification above Cyber Essentials, judged where Microsoft 365 can speak to it.

IASME Cyber Assurance covers the whole organisation: governance, risk, policies, people, data protection,
premises, backups, monitoring, change, continuity and incidents, on top of the Cyber Essentials technical
controls it requires first. Level 1 is a self-assessment verified by an assessor; Level 2 adds an audit of the
same answers. Most of it is about the organisation's own processes, so most controls here carry the text to
fill in; the ones Microsoft 365 evidences are judged from the checks. The requirements are paraphrased from
IASME's question set by theme; question numbers change between versions, so each answer is matched to its
question by wording.
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

GOVERNANCE = Theme(
    "governance",
    "Organisation and governance",
    "A named director is accountable for information security and data protection, it's on the board's agenda, "
    "and someone is given the time and skills to run it.",
    (
        Control(
            "responsibility",
            "",
            "A named person accountable for security",
            "A board member, director or partner has overall responsibility for information security and data "
            "protection, and it is a standing item at management meetings.",
            topic="security_responsibility",
            scope=OUTSIDE,
        ),
        Control(
            "policies",
            "",
            "Security policies written, approved and reviewed",
            "Information security policies cover the scope, set clear responsibilities, are shared with everyone "
            "they apply to, and have been reviewed and approved in the last 12 months.",
            topic="policies",
            scope=OUTSIDE,
        ),
        Control(
            "risk_assessment",
            "",
            "A current risk assessment with owners and actions",
            "A risk assessment covering the scope, reviewed in the last 12 months, names an owner and a treatment for "
            "each risk and has an action plan approved at board level.",
            topic="risk_assessment",
            scope=OUTSIDE,
        ),
    ),
)

SUPPLY_CHAIN = Theme(
    "supply_chain",
    "Cloud services and the supply chain",
    "The cloud services that hold the organisation's data are known, where they keep it is known, and suppliers "
    "are held to security requirements.",
    (
        Control(
            "cloud_services",
            "",
            "Cloud services in use listed",
            "The cloud services used to store and share information are listed.",
            topic="cloud_services",
            scope="Microsoft 365 lists the apps connected to its sign-in; add services people sign in to separately.",
        ),
        Control(
            "data_location",
            "",
            "Where cloud data is stored",
            "The organisation knows where its cloud providers store its data.",
            topic="data_location",
            scope="Judged from the tenant's country in Microsoft 365. Add every other cloud service.",
        ),
        Control(
            "apps",
            "",
            "Third-party apps controlled",
            "Users can't connect third-party apps to company data on their own, and apps with broad access are known.",
            rules=("user_consent", "risky_app_permissions", "app_registration"),
            topic="third_party_apps",
            report="apps",
            scope="Judged from Entra ID consent settings and app permissions.",
        ),
        Control(
            "supplier_security",
            "",
            "Security requirements for suppliers",
            "Suppliers and contractors who handle the organisation's information must meet defined security "
            "requirements, checked before they start and reviewed.",
            topic="supplier_security",
            scope=OUTSIDE,
        ),
    ),
)

ASSETS = Theme(
    "assets",
    "Information assets",
    "Equipment and information are listed with owners, sensitive and personal data is identified and encrypted, "
    "and data is wiped before equipment goes.",
    (
        Control(
            "asset_register",
            "",
            "An asset register with named owners",
            "Physical and information assets, including every laptop, tablet and phone, are recorded with a named "
            "owner, a category and their location.",
            topic="asset_register",
            support=("stale_devices", "intune_enrolled"),
            report="devices",
            figures=figures_devices,
            scope="Microsoft 365 lists the devices Intune and Entra ID know; the register covers everything else.",
        ),
        Control(
            "encryption",
            "",
            "Data encrypted in the cloud, in transit and on laptops",
            "Data is encrypted on its way to and while stored in cloud services, and on mobile devices that hold it.",
            rules=("device_encryption",),
            topic="encryption",
            support=("mta_sts",),
            report="devices",
            scope="Judged from Intune for company computers; Microsoft encrypts Microsoft 365 data itself.",
        ),
        Control(
            "removable_media",
            "",
            "Removable media tracked and encrypted",
            "USB drives and other removable media are controlled, recorded and encrypted.",
            topic="removable_media",
            scope=OUTSIDE,
        ),
        Control(
            "data_review",
            "",
            "Data reviewed and deleted when no longer needed",
            "The data held is reviewed regularly and deleted when it's no longer relevant.",
            topic="data_retention",
            scope=OUTSIDE,
        ),
        Control(
            "disposal",
            "",
            "Data wiped before equipment is disposed of",
            "When assets are no longer needed, data is securely wiped or the storage destroyed.",
            topic="secure_disposal",
            scope=OUTSIDE,
        ),
    ),
)

DATA_PROTECTION = Theme(
    "data_protection",
    "Legal compliance and data protection",
    "UK GDPR is met: the ICO fee is paid, someone manages data protection, personal data is recorded with its "
    "lawful basis, the privacy notice is current, rights requests are handled and processors are under contract.",
    (
        Control(
            "ico_registration",
            "",
            "ICO data protection fee paid",
            "The organisation is registered with the ICO, or has recorded why it's exempt.",
            topic="ico_registration",
            scope=OUTSIDE,
        ),
        Control(
            "dpo",
            "",
            "Data protection managed by a named person",
            "A Data Protection Officer is appointed where one is required; otherwise the decision is recorded and a "
            "named person manages data protection.",
            topic="dpo",
            scope=OUTSIDE,
        ),
        Control(
            "ropa",
            "",
            "Personal data recorded with its lawful basis",
            "For each kind of personal and special category data held, the purpose and lawful basis are recorded.",
            topic="ropa",
            scope=OUTSIDE,
        ),
        Control(
            "privacy_notice",
            "",
            "A current privacy notice",
            "A privacy notice says what data is collected and why, how it's used and people's rights, and is reviewed.",
            topic="privacy_notice",
            scope=OUTSIDE,
        ),
        Control(
            "rights",
            "",
            "People's rights requests handled",
            "There is a process for requests to see, correct or delete personal data, and for withdrawing consent.",
            topic="data_subject_rights",
            scope=OUTSIDE,
        ),
        Control(
            "processors",
            "",
            "Data processing agreements with suppliers",
            "Every supplier that processes personal data has a contract with the data protection terms UK GDPR "
            "requires, including for data held outside the UK.",
            topic="processor_contracts",
            scope=OUTSIDE,
        ),
    ),
)

PEOPLE = Theme(
    "people",
    "People",
    "Staff are vetted, given only the access their job needs, briefed and trained, and their access goes when they "
    "leave or change role.",
    (
        Control(
            "vetting",
            "",
            "Staff checked before they get access",
            "Everyone with access to the organisation's data is checked as suitable first.",
            topic="staff_vetting",
            scope=OUTSIDE,
        ),
        Control(
            "least_privilege",
            "",
            "Access limited to what each job needs",
            "People only have access to the systems and data their role needs.",
            rules=("guest_access_level", "guest_invites"),
            topic="least_privilege",
            report="guests",
            scope="Microsoft 365 shows what guests can see and who can invite them; describe how staff access is set.",
        ),
        Control(
            "training",
            "",
            "Security and data protection training",
            "Staff and contractors are briefed on their responsibilities when they start and trained regularly.",
            topic="training",
            scope=OUTSIDE,
        ),
        Control(
            "leavers",
            "",
            "Access removed when people leave",
            "When people leave or change role, their access is removed or changed promptly.",
            rules=("stale_users", "licensed_disabled", "pending_guests"),
            topic="leavers",
            report="inactive",
            scope="Judged from sign-in activity in Microsoft Entra ID. Describe the leavers process in the answer.",
        ),
    ),
)

PHYSICAL = Theme(
    "physical",
    "Physical and environmental protection",
    "Premises are secured, only authorised people get in, and networks are restricted to authorised devices.",
    (
        Control(
            "premises",
            "",
            "Premises secured",
            "Business premises in scope are physically protected, with access limited to authorised people.",
            topic="physical_security",
            scope=OUTSIDE,
        ),
    ),
)

INTRUSION = Theme(
    "intrusion",
    "Preventing intrusion",
    "Accounts are protected by MFA, sessions and devices lock, vulnerabilities are found by scanning and fixed, "
    "and malicious sites are blocked.",
    (
        Control(
            "mfa",
            "",
            "MFA on every cloud account",
            "Multi-factor authentication protects every account that can reach the organisation's cloud services.",
            rules=("mfa_enforced", "admins_ca_mfa", "legacy_auth_blocked"),
            topic="mfa_remote",
            support=("users_mfa_registered", "phone_only_mfa"),
            report="mfa",
            figures=figures_mfa_users,
            scope="Judged for Microsoft 365 sign-ins. Add every other cloud service.",
        ),
        Control(
            "admins",
            "",
            "Few, separate, named administrators",
            "Administrator access is limited to the people who need it, through separate accounts.",
            rules=("global_admin_count", "guest_admins", "service_principal_admins", "cloud_only_admins"),
            topic="admin_count",
            support=("admin_phishing_resistant",),
            report="admins",
            figures=figures_admins,
            scope="Microsoft 365 shows who holds admin roles today; add administrators of other systems.",
        ),
        Control(
            "device_lock",
            "",
            "Devices and sessions don't stay signed in",
            "Accounts and devices lock or sign out after a period of inactivity.",
            topic="device_lock",
            scope=OUTSIDE,
        ),
        Control(
            "vulnerability_scanning",
            "",
            "Vulnerability scans at least every six months",
            "All systems are scanned for vulnerabilities at least every six months and after major changes, with "
            "penetration tests where the risk assessment calls for them, and the findings are fixed.",
            topic="vulnerability_scanning",
            scope=OUTSIDE,
        ),
        Control(
            "web_filtering",
            "",
            "Malicious websites blocked",
            "Access to malicious internet sites and domains is blocked at the boundary or on the device.",
            topic="web_filtering",
            scope=OUTSIDE,
        ),
    ),
)

BACKUP = Theme(
    "backup",
    "Backup and restore",
    "Information is backed up at least weekly, kept apart and protected, with a copy somewhere else, and restores "
    "are tested.",
    (
        Control(
            "backups",
            "",
            "Regular, separate, tested backups",
            "All information is backed up regularly, backups are protected and kept in a different location, and "
            "restores are tested.",
            topic="backups",
            scope="Microsoft 365 doesn't back itself up for this purpose; describe the backup of Microsoft 365 too.",
        ),
    ),
)

MONITORING = Theme(
    "monitoring",
    "Monitoring",
    "Access attempts are watched, logs are kept safe for a set time, and warnings are reviewed.",
    (
        Control(
            "logging",
            "",
            "Logs and audit trails kept",
            "Event logs and audit trails are kept securely for a defined period.",
            rules=("mailbox_auditing",),
            topic="audit_logs",
            report="tenant-settings",
            scope="Judged from Exchange Online mailbox auditing and the unified audit log.",
        ),
        Control(
            "access_monitoring",
            "",
            "Sign-in attempts watched",
            "The organisation watches who is trying to access its information and from where.",
            rules=("risky_users", "risky_sign_ins"),
            topic="risk_detection",
            report="risky-users",
            scope="Judged from Entra ID Protection (needs Entra ID P2 for the full detections).",
        ),
        Control(
            "log_review",
            "",
            "Warnings and reports reviewed",
            "Security warnings and reports are reviewed regularly and security settings are rechecked.",
            topic="log_review",
            support=("suspicious_inbox_rules", "domain_federation_changed"),
            scope="Microsoft 365 shows what's logged and flagged; describe who reviews it and how often.",
        ),
    ),
)

CHANGE = Theme(
    "change",
    "Change management",
    "Changes are planned, tested and approved, only approved software is installed, and new uses of personal data "
    "get an impact assessment first.",
    (
        Control(
            "change_control",
            "",
            "Changes approved and tested",
            "Changes to systems, applications and networks are recorded, tested and approved before they're made.",
            topic="change_management",
            scope=OUTSIDE,
        ),
        Control(
            "approved_software",
            "",
            "Only approved software installed",
            "Computers and servers are set up only with approved software, and software nobody needs is removed.",
            topic="software_removal",
            scope=OUTSIDE,
        ),
        Control(
            "dpia",
            "",
            "Impact assessments before high-risk processing",
            "A data protection impact assessment is carried out before high-risk processing starts.",
            topic="dpia",
            scope=OUTSIDE,
        ),
    ),
)

CONTINUITY = Theme(
    "continuity",
    "Business continuity",
    "Continuity and disaster recovery plans exist, are reviewed and tested every year, and are signed off.",
    (
        Control(
            "bcp_dr",
            "",
            "Continuity and recovery plans tested yearly",
            "Business impact assessments and continuity and recovery plans are in place, reviewed and tested at "
            "least once a year, and approved at board level.",
            topic="bcp_dr",
            scope=OUTSIDE,
        ),
    ),
)

INCIDENTS = Theme(
    "incidents",
    "Incident management",
    "Incidents are reported, recorded and investigated, the right outside bodies are told, and lessons are learned.",
    (
        Control(
            "incident_response",
            "",
            "Incidents reported, investigated and learned from",
            "Security incidents and suspected weaknesses are reported and recorded, investigated for their cause, "
            "and the lessons fed back, with clear roles for everyone involved.",
            topic="incident_response",
            scope=OUTSIDE,
        ),
        Control(
            "compromise",
            "",
            "A compromised account is contained",
            "There is a process for when an account or password may be compromised.",
            topic="compromise_process",
            scope=OUTSIDE,
        ),
        Control(
            "external_reporting",
            "",
            "Breaches reported to the ICO when required",
            "Incidents are reported to outside bodies as required, including personal data breaches to the ICO.",
            topic="breach_reporting",
            scope=OUTSIDE,
        ),
    ),
)

CYBER_ASSURANCE = register(
    Scheme(
        "iasme-cyber-assurance",
        "IASME Cyber Assurance",
        "Cyber Assurance",
        "IASME",
        "uk",
        "The controls IASME Cyber Assurance asks about, theme by theme: what Microsoft 365 shows judged from the "
        "data, and the rest as text to complete. Hold Cyber Essentials first; its pack covers the technical "
        "controls.",
        (
            GOVERNANCE,
            SUPPLY_CHAIN,
            ASSETS,
            DATA_PROTECTION,
            PEOPLE,
            PHYSICAL,
            INTRUSION,
            BACKUP,
            MONITORING,
            CHANGE,
            CONTINUITY,
            INCIDENTS,
        ),
        "IASME Cyber Assurance, Level 1 (verified self-assessment) and Level 2 (audit)",
        "Requirements paraphrased by theme from IASME's Cyber Assurance question set. Question numbers change between "
        "versions, so match each answer to its question by wording, and check the current question set in the IASME "
        "portal before you submit.",
        "https://iasme.co.uk/iasme-cyber-assurance/",
        strict=True,
        numbered=False,
        audit=(
            Audit(
                "Level 2: documents and interviews",
                "At Level 2 an auditor asks to see the evidence behind the Level 1 answers (policies, the risk "
                "assessment, the asset register, training records, the breach log) and interviews staff.",
                "Microsoft 365 can show the technical evidence on the day: the MFA, admin, logging and leavers "
                "controls on this page, with the rows behind them.",
                ("mfa", "admins", "logging", "leavers"),
            ),
        ),
        notes=(
            "Cyber Essentials, or IASME Cyber Baseline, must be held before applying; use the Cyber Essentials pack "
            "for the technical controls.",
            "The answers must be approved by a director, partner, owner or trustee before they're submitted.",
            "Scope: Cyber Assurance covers the whole organisation or a named business unit, including paper records. "
            "A Pass here covers Microsoft 365; the answer should say what covers the rest.",
        ),
    )
)
