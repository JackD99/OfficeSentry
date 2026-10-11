"""NIST Cybersecurity Framework 2.0: the outcomes Microsoft 365 evidences, by function and category.

CSF 2.0 (February 2024) organises cybersecurity outcomes under six functions: Govern, Identify, Protect,
Detect, Respond and Recover, each split into categories (PR.AA, DE.CM, ...). It's an outcome framework, not
a certification, so it's often what a US client or a group's head office asks a UK business to map to. This
pack judges the categories Microsoft 365 data speaks to and gives text for the rest. Category identifiers are
NIST's; the outcomes are paraphrased.
"""

from __future__ import annotations

from . import evidence_topics  # noqa: F401  (the topics the controls name must be registered first)
from .schemes import Control, Scheme, Theme, figures_admins, figures_devices, figures_mfa_users, register

OUTSIDE = "All of this. Answer from the organisation's own records."

GOVERN = Theme(
    "govern",
    "Govern (GV)",
    "Roles, policy, risk management and supply chain risk are set and overseen by leadership.",
    (
        Control(
            "gv_rr",
            "GV.RR",
            "Roles, responsibilities and authorities",
            "Leadership is accountable for cybersecurity, and roles are set and resourced.",
            topic="security_responsibility",
            scope=OUTSIDE,
        ),
        Control(
            "gv_po",
            "GV.PO",
            "Policy",
            "Cybersecurity policy is set, communicated, enforced and reviewed.",
            topic="policies",
            scope=OUTSIDE,
        ),
        Control(
            "gv_rm",
            "GV.RM",
            "Risk management strategy",
            "Risk appetite and priorities are set and used to make decisions.",
            topic="risk_assessment",
            scope=OUTSIDE,
        ),
        Control(
            "gv_sc",
            "GV.SC",
            "Supply chain risk management",
            "Suppliers are known, assessed and bound by security requirements, and the cloud apps that reach company "
            "data are controlled.",
            rules=("user_consent", "risky_app_permissions"),
            topic="supplier_security",
            report="apps",
            scope="Microsoft 365 shows which apps can reach its data; the supplier reviews are the organisation's own.",
        ),
    ),
)

IDENTIFY = Theme(
    "identify",
    "Identify (ID)",
    "Assets are known and risks to them are understood and improved on.",
    (
        Control(
            "id_am",
            "ID.AM",
            "Asset management",
            "Hardware, software, services and data are inventoried, and stale entries are removed.",
            rules=("stale_devices",),
            topic="asset_register",
            report="devices",
            figures=figures_devices,
            scope="Microsoft 365 lists the devices Intune and Entra ID know; the inventory covers everything else.",
        ),
        Control(
            "id_ra",
            "ID.RA",
            "Risk assessment",
            "Vulnerabilities are identified, including systems out of vendor support, and risks are assessed.",
            rules=("supported_windows",),
            topic="vulnerability_scanning",
            report="devices",
            scope="Judged from the Windows versions Intune reports. Describe scanning of everything else.",
        ),
    ),
)

PROTECT = Theme(
    "protect",
    "Protect (PR)",
    "Identities are verified and least privilege enforced, people trained, data protected, platforms hardened and "
    "backed up.",
    (
        Control(
            "pr_aa_mfa",
            "PR.AA",
            "Identity, authentication and access: MFA",
            "Users are authenticated in proportion to risk: multi-factor authentication for everyone, and legacy "
            "protocols blocked.",
            rules=("mfa_enforced", "admins_ca_mfa", "legacy_auth_blocked"),
            topic="mfa_remote",
            support=("admin_phishing_resistant", "phone_only_mfa"),
            report="mfa",
            figures=figures_mfa_users,
            scope="Judged for Microsoft 365 sign-ins. Add other systems.",
        ),
        Control(
            "pr_aa_privilege",
            "PR.AA",
            "Identity, authentication and access: least privilege",
            "Access rights are managed with least privilege, admin rights are few, and leavers lose access.",
            rules=("global_admin_count", "guest_admins", "service_principal_admins", "stale_users"),
            topic="admin_count",
            report="admins",
            figures=figures_admins,
            scope="Judged from Entra ID roles and sign-in activity.",
        ),
        Control(
            "pr_at",
            "PR.AT",
            "Awareness and training",
            "People are trained to do their work with security in mind.",
            topic="training",
            scope=OUTSIDE,
        ),
        Control(
            "pr_ds",
            "PR.DS",
            "Data security",
            "Data is protected at rest and in transit, and isn't shared or forwarded outside unnoticed.",
            rules=("device_encryption", "anyone_links", "external_forwarding", "auto_forward_policy"),
            topic="encryption",
            support=("mta_sts", "dmarc_policy"),
            report="devices",
            scope="Judged from Intune encryption, SharePoint sharing and Exchange forwarding.",
        ),
        Control(
            "pr_ps",
            "PR.PS",
            "Platform security",
            "Hardware and software are managed, configured securely and kept updated, and logs are generated.",
            rules=("intune_enrolled", "device_compliance", "mailbox_auditing", "app_registration", "tenant_creation"),
            topic="devices_managed",
            report="devices",
            figures=figures_devices,
            scope="Judged from Intune, Exchange Online auditing and Entra ID tenant settings.",
        ),
        Control(
            "pr_ir",
            "PR.IR",
            "Technology infrastructure resilience",
            "Networks are protected and capacity and backups keep services running.",
            topic="backups",
            support=("sharepoint_storage_space",),
            scope="Microsoft 365 doesn't back itself up for this purpose; describe the backup of Microsoft 365 too.",
        ),
    ),
)

DETECT = Theme(
    "detect",
    "Detect (DE)",
    "Activity is monitored and adverse events are analysed.",
    (
        Control(
            "de_cm",
            "DE.CM",
            "Continuous monitoring",
            "Sign-ins, accounts and mailboxes are monitored for anomalies.",
            rules=("risky_users", "risky_sign_ins", "suspicious_inbox_rules"),
            topic="risk_detection",
            support=("domain_federation_changed",),
            report="risky-users",
            scope="Judged from Entra ID Protection and inbox rules (Entra ID P2 for the full detections).",
        ),
        Control(
            "de_ae",
            "DE.AE",
            "Adverse event analysis",
            "Alerts are analysed to understand what happened and its impact.",
            topic="log_review",
            scope=OUTSIDE,
        ),
    ),
)

RESPOND = Theme(
    "respond",
    "Respond (RS)",
    "Incidents are managed, analysed, reported and contained.",
    (
        Control(
            "rs_ma",
            "RS.MA",
            "Incident management",
            "An incident response plan is run when an incident is declared.",
            topic="incident_response",
            scope=OUTSIDE,
        ),
        Control(
            "rs_mi",
            "RS.MI",
            "Incident mitigation",
            "Incidents are contained, for example by securing a compromised account.",
            topic="compromise_process",
            scope=OUTSIDE,
        ),
        Control(
            "rs_co",
            "RS.CO",
            "Incident reporting and communication",
            "Incidents are reported to the people and authorities who must be told.",
            topic="breach_reporting",
            scope=OUTSIDE,
        ),
    ),
)

RECOVER = Theme(
    "recover",
    "Recover (RC)",
    "Recovery plans are run and restored data is checked.",
    (
        Control(
            "rc_rp",
            "RC.RP",
            "Incident recovery plan execution",
            "Systems and data are restored from backups by a tested plan.",
            topic="bcp_dr",
            scope=OUTSIDE,
        ),
    ),
)

NIST_CSF = register(
    Scheme(
        "nist-csf",
        "NIST Cybersecurity Framework 2.0",
        "NIST CSF",
        "NIST",
        "international",
        "The CSF 2.0 categories Microsoft 365 data speaks to, judged from the checks, and text for the rest. Use it "
        "when a client or a parent company asks for a CSF mapping or profile.",
        (GOVERN, IDENTIFY, PROTECT, DETECT, RESPOND, RECOVER),
        "NIST Cybersecurity Framework (CSF) 2.0, February 2024",
        "Category identifiers are NIST's; outcomes are paraphrased. A CSF profile is the organisation's own: this "
        "pack is the Microsoft 365 evidence for it.",
        "https://www.nist.gov/cyberframework",
        order=30,
        notes=(
            "The CSF has no pass mark. Use Pass as evidence a category's outcome is achieved for Microsoft 365, and "
            "Fail and Partly as the gaps for the target profile.",
        ),
    )
)
