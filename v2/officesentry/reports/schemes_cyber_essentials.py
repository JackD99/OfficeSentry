"""Cyber Essentials and Cyber Essentials Plus: the five control themes, judged from Microsoft 365 data.

Requirements are paraphrased from the NCSC and IASME *Cyber Essentials: Requirements for IT Infrastructure*
v3.3 and IASME's self-assessment question set "Danzell" (version 16, April 2026, for assessments bought from
27 April 2026), never copied. Each control names the question it answers in the IASME portal, so the
engineer can go from a verdict to the form.

Cyber Essentials is all or nothing: a control Microsoft 365 can judge with any gap is a Fail here, and
the questions the assessment fails outright on are marked. What Microsoft 365 can't see (firewalls, patch
timing, anti-malware settings, processes) is "Your evidence", with a text to complete and save.
"""

from __future__ import annotations

from . import evidence_topics  # noqa: F401  (the topics the controls name must be registered first)
from .schemes import Audit, Control, Scheme, Theme, figures_admins, figures_devices, figures_mfa_users, register

FIREWALLS_SCOPE = (
    "Firewalls aren't part of Microsoft 365. Intune can enforce the Windows firewall, but reading its "
    "configuration profiles needs a permission the app doesn't hold, so answer from the firewall itself."
)
NOT_READ = "Office Sentry doesn't read device configuration profiles from Intune, so answer from the Intune policy."

FIREWALLS = Theme(
    "firewalls",
    "Firewalls",
    "Every device and network in scope sits behind a firewall that blocks unauthenticated inbound "
    "connections by default, with changed passwords, reviewed rules and no administration from the internet.",
    (
        Control(
            "firewall_boundary",
            "A4.1",
            "A firewall at every boundary with the internet",
            "A boundary firewall (or the router's firewall) protects each internet connection, and home workers "
            "are protected by the firewall on their device.",
            topic="firewall_boundary",
            scope=FIREWALLS_SCOPE,
        ),
        Control(
            "software_firewall",
            "A4.1.1",
            "Software firewalls on every computer and server",
            "The built-in firewall is turned on for every laptop, desktop and server.",
            topic="software_firewall",
            scope=FIREWALLS_SCOPE,
        ),
        Control(
            "firewall_passwords",
            "A4.2",
            "Default firewall passwords changed",
            "The default administrator password of every router and firewall was changed to a strong one.",
            topic="firewall_passwords",
            scope=FIREWALLS_SCOPE,
        ),
        Control(
            "firewall_review",
            "A4.6",
            "Firewall rules reviewed in the last 12 months",
            "Inbound rules are reviewed and removed when no longer needed, at least yearly.",
            topic="firewall_review",
            scope=FIREWALLS_SCOPE,
        ),
        Control(
            "firewall_inbound",
            "A4.7",
            "Unauthenticated inbound connections blocked by default",
            "The firewall blocks inbound connections unless an approved, documented rule allows one (A4.8).",
            topic="firewall_inbound",
            scope=FIREWALLS_SCOPE,
        ),
        Control(
            "firewall_remote_admin",
            "A4.9",
            "No firewall administration from the internet",
            "The firewall's administration interface can't be reached from the internet, or only with MFA or "
            "from an allow list of trusted addresses with a documented need (A4.10, A4.11).",
            topic="firewall_remote_admin",
            scope=FIREWALLS_SCOPE,
        ),
    ),
)

SECURE_CONFIGURATION = Theme(
    "secure-configuration",
    "Secure configuration",
    "Devices and cloud services run only the software, services and accounts they need, with default "
    "passwords changed, automatic running of files off and devices that lock.",
    (
        Control(
            "unused_software",
            "A5.1",
            "Unused software and services removed from devices",
            "Software and services nobody uses are removed or turned off on every device.",
            topic="software_removal",
            scope="Microsoft 365 doesn't report what's installed on devices.",
        ),
        Control(
            "m365_services",
            "A5.1 (cloud services)",
            "Unneeded sign-in protocols turned off in Microsoft 365",
            "Services that aren't needed are turned off in cloud services too: for Microsoft 365, the legacy "
            "sign-in protocols that can't use MFA, and SMTP AUTH on mailboxes that don't send mail that way.",
            rules=("legacy_auth_blocked", "smtp_auth"),
            report="legacy-auth",
            scope="Judged from Conditional Access and Exchange Online settings.",
        ),
        Control(
            "needed_accounts",
            "A5.2",
            "Only the accounts that are needed",
            "Devices and cloud services hold only the user accounts in use: no leftover licensed-but-disabled "
            "accounts and no forgotten guest invitations.",
            rules=("licensed_disabled", "pending_guests"),
            strict=False,
            report="inactive",
            scope="Judged from Microsoft Entra ID accounts and guest invitations. Add local accounts on devices "
            "and servers.",
        ),
        Control(
            "default_passwords",
            "A5.3",
            "Default passwords changed on every device and account",
            "Default and built-in account passwords are changed at set-up.",
            topic="default_passwords",
            scope="Microsoft 365 accounts have no default password; this is about devices and built-in accounts.",
        ),
        Control(
            "autorun",
            "A5.8",
            "Automatic running of downloaded files turned off",
            "AutoRun and AutoPlay are off, and downloaded files don't run without the user's say-so.",
            topic="autorun",
            scope=NOT_READ,
        ),
        Control(
            "device_lock",
            "A5.9",
            "Devices lock and need a PIN, password or biometric",
            "Every device locks after inactivity and unlocks with a password, PIN of at least six digits or "
            "biometric (A5.10), with brute-force protection.",
            topic="device_lock",
            scope=NOT_READ,
        ),
    ),
)

UPDATES = Theme(
    "updates",
    "Security update management",
    "Every operating system and application is supported by its vendor, licensed, set to update "
    "automatically where possible, and has high-risk and critical updates installed within 14 days.",
    (
        Control(
            "supported_os",
            "A6.1",
            "Every operating system still supported by its vendor",
            "All operating systems in scope receive regular security updates from their vendor; Windows 10 "
            "needs Extended Security Updates after October 2025.",
            rules=("supported_windows",),
            topic="devices_patched",
            report="devices",
            table="Operating systems",
            figures=figures_devices,
            scope="Judged for Windows computers known to Intune or Entra ID. macOS, Linux, phones, servers "
            "not joined to Entra ID and router or firewall firmware aren't seen.",
        ),
        Control(
            "supported_software",
            "A6.2",
            "Every application supported and licensed",
            "Browsers, anti-malware, email and office applications are supported versions (A6.2.1 to A6.2.4), "
            "nothing is unlicensed or unsupported (A6.3, A6.6), and any unsupported software is on a separate "
            "sub-set (A6.7).",
            topic="software_supported",
            scope="Microsoft 365 doesn't report installed application versions. Intune's discovered apps would "
            "need a permission the app doesn't hold.",
        ),
        Control(
            "os_updates_14_days",
            "A6.4",
            "Operating system updates within 14 days",
            "High-risk and critical security updates for operating systems, routers and firewalls are installed "
            "within 14 days of release, by automatic updates where possible (A6.4.1, A6.4.2).",
            topic="patching_14_days",
            support=("device_checkin",),
            autofail=True,
            scope="Intune shows which Windows release each computer runs and when it last checked in, not "
            "when updates were installed.",
        ),
        Control(
            "app_updates_14_days",
            "A6.5",
            "Application updates within 14 days",
            "High-risk and critical application updates are installed within 14 days of release, by automatic "
            "updates where possible (A6.5.1, A6.5.2).",
            topic="app_updates",
            autofail=True,
            scope="Microsoft 365 doesn't see application versions on devices.",
        ),
    ),
)

ACCESS = Theme(
    "access",
    "User access control",
    "Accounts are created through a process, are unique to a person, hold only the access the job needs, "
    "are removed when people leave, use separate accounts for administration, strong passwords and MFA on "
    "every cloud service.",
    (
        Control(
            "account_approval",
            "A7.1",
            "Accounts created only through an approval process",
            "A user account is created only after a request has been approved and recorded.",
            topic="account_approval",
            scope="A process Microsoft 365 doesn't show. The directory audit log records who created each account.",
        ),
        Control(
            "unique_accounts",
            "A7.2",
            "Every account used by one person, with unique credentials",
            "User and administrator accounts are each tied to a person: no shared sign-ins.",
            rules=("shared_mailbox_signin",),
            topic="shared_accounts",
            fix="Block sign-in on every shared mailbox's account and give each person their own account.",
            report="shared-mailboxes",
            scope="Judged from whether shared mailboxes can be signed in to. Check devices and other systems for "
            "shared logins.",
        ),
        Control(
            "leavers",
            "A7.3",
            "Leavers' accounts disabled or deleted",
            "Accounts of people who have left are disabled or deleted promptly, and no account sits unused.",
            rules=("stale_users",),
            topic="leavers",
            report="inactive",
            table="Inactive members",
            scope="Judged from sign-in activity in Microsoft Entra ID (needs Entra ID P1 for last sign-in dates). "
            "Describe the leavers process in the answer.",
        ),
        Control(
            "least_privilege",
            "A7.4",
            "Only the access the job needs",
            "Staff have the access their current role needs and no more, through role-based groups.",
            topic="least_privilege",
            scope="A process Microsoft 365 doesn't show; the group membership and admin role reports help review it.",
        ),
        Control(
            "admin_process",
            "A7.5",
            "A formal process for giving administrator access",
            "Administrator access is granted only after approval, to a named person, and recorded.",
            topic="admin_process",
            scope="A process Microsoft 365 doesn't show.",
        ),
        Control(
            "separate_admins",
            "A7.6",
            "Separate accounts for administration",
            "Administration is done from separate admin accounts that aren't used for email, browsing or "
            "everyday work (A7.6, A7.7).",
            topic="separate_admins",
            fix="Give each administrator a separate, unlicensed admin account with no mailbox, used only for "
            "administration, and take the admin roles off the account they use every day.",
            report="admins",
            table="Privileged role holders",
            scope="Judged from Microsoft 365 admin role holders: an admin account with a licence or a mailbox "
            "looks like an everyday account. Add local and domain administrators on devices and servers.",
        ),
        Control(
            "admin_tracking",
            "A7.8",
            "Administrator accounts tracked and reviewed",
            "The organisation knows which accounts hold administrator access (A7.8) and reviews the list "
            "regularly (A7.9).",
            topic="admin_count",
            rules=("global_admin_count", "guest_admins", "service_principal_admins"),
            fix="Keep a register of administrator accounts, review it at least quarterly and remove roles nobody "
            "needs.",
            strict=False,
            report="admins",
            figures=figures_admins,
            scope="Microsoft 365 shows who holds admin roles today; describe how often the list is reviewed and "
            "by whom.",
        ),
        Control(
            "passwords",
            "A7.10",
            "Passwords protected from guessing, with quality controls",
            "Passwords are protected from brute-force guessing (A7.10: MFA, throttling or lockout) and their "
            "quality is managed technically (A7.11: MFA, 12 characters, or 8 characters with a deny list).",
            topic="password_policy",
            fix="Turn on MFA for everyone, which satisfies both questions, and keep Entra ID's banned password "
            "list and smart lockout on.",
            report="auth-methods",
            scope="Microsoft Entra ID's defaults (8 characters, a banned password list and smart lockout) aren't "
            "read from the tenant. Add devices, servers and other systems that take passwords.",
        ),
        Control(
            "compromise_process",
            "A7.13",
            "A process for a compromised password or account",
            "When a password or account may be compromised, it's reset and the account checked promptly.",
            topic="compromise_process",
            scope="A process Microsoft 365 doesn't show; Entra ID Protection's risk detections support it.",
        ),
        Control(
            "mfa_available",
            "A7.14",
            "MFA available on every cloud service",
            "Every cloud service in use offers MFA, or is linked to one that does; any that don't are listed (A7.15).",
            topic="mfa_available",
            scope="Microsoft 365 offers MFA through Entra ID. List the other cloud services and check each.",
        ),
        Control(
            "mfa_admins",
            "A7.16",
            "MFA for every administrator of cloud services",
            "Every administrator account on every cloud service uses MFA with a password of at least 8 characters. "
            "The assessment fails on a No.",
            topic="mfa_privileged",
            support=("admin_mfa",),
            fix="Require MFA for every admin role with a Conditional Access policy (or turn on Security Defaults), "
            "with only a named break-glass account excluded, and have each admin register a method.",
            autofail=True,
            report="admins",
            table="Privileged role holders",
            figures=figures_admins,
            scope="Judged for Microsoft 365 and Entra ID admin roles, from Conditional Access enforcement with "
            "named break-glass accounts allowed. Add administrators of other cloud services.",
        ),
        Control(
            "mfa_users",
            "A7.17",
            "MFA for every user of cloud services",
            "Every user of every cloud service uses MFA with a password of at least 8 characters. The assessment "
            "fails on a No.",
            topic="mfa_remote",
            support=("users_mfa_registered",),
            fix="Require MFA for all users and all apps with a Conditional Access policy (or turn on Security "
            "Defaults), with only a named break-glass account excluded, then chase everyone not yet registered.",
            autofail=True,
            report="mfa",
            table="Accounts with a gap",
            figures=figures_mfa_users,
            scope="Judged for Microsoft 365 sign-ins from Conditional Access enforcement (or Security Defaults), "
            "with named break-glass accounts allowed. Add other cloud services.",
        ),
    ),
)

MALWARE = Theme(
    "malware",
    "Malware protection",
    "Every computer, tablet and phone is protected by anti-malware software that updates and scans, "
    "or only runs approved applications from a managed store.",
    (
        Control(
            "anti_malware",
            "A8.1",
            "Anti-malware on every computer, tablet and phone",
            "Each device has anti-malware software that updates itself, scans files on access and blocks "
            "malicious websites (A8.2, A8.3), or installs apps only from an approved store or allow list "
            "(A8.4, A8.5).",
            topic="anti_malware",
            support=("device_compliance",),
            scope="Office Sentry doesn't read Microsoft Defender or anti-malware settings. Intune compliance "
            "supports the answer only if the compliance policy requires anti-malware; check the policy.",
        ),
    ),
)

THEMES = (FIREWALLS, SECURE_CONFIGURATION, UPDATES, ACCESS, MALWARE)
VERSION = (
    "Cyber Essentials: Requirements for IT Infrastructure v3.3 (NCSC and IASME, April 2026) and IASME "
    "question set Danzell, version 16 (April 2026), for assessments bought from 27 April 2026"
)
SOURCE = (
    "Requirements paraphrased from the NCSC and IASME Cyber Essentials documents; question numbers from "
    "IASME's self-assessment preparation booklet, checked 10 October 2026. IASME publishes a new question set "
    "most Aprils: check the portal's numbers before submitting."
)
SOURCE_URL = "https://iasme.co.uk/cyber-essentials/preview-the-self-assessment-questions-for-cyber-essentials/"
NOTES = (
    "Scope: Cyber Essentials covers every device that reaches organisational data or services, every cloud "
    "service holding the organisation's data, and home workers' devices. Microsoft 365 is one cloud service "
    "in that scope, so a Pass here is evidence for the form, not the whole answer.",
    "The answers must be approved by a board member or the business owner before submission.",
)

CYBER_ESSENTIALS = register(
    Scheme(
        "cyber-essentials",
        "Cyber Essentials",
        "Cyber Essentials",
        "NCSC and IASME",
        "uk",
        "The five Cyber Essentials control themes, judged from Microsoft 365 data as an assessor would: any gap "
        "is a Fail. Fix the failing controls, write the evidence Microsoft 365 can't show, then copy the "
        "answers into the IASME portal.",
        THEMES,
        version=VERSION,
        source=SOURCE,
        source_url=SOURCE_URL,
        strict=True,
        questions="cyber-essentials",
        notes=NOTES,
        renewal="12 months",
    )
)

CE_PLUS_TESTS = (
    Audit(
        "Test 1: Remote vulnerability assessment",
        "The assessor scans every internet-facing IP address the organisation uses and fails any service with a "
        "high or critical vulnerability, or that lets someone in without authentication.",
        "Microsoft 365 has no part in this test; it's about the firewall, routers and anything published "
        "to the internet. Have the firewall review and inbound rules ready.",
        ("firewall_inbound", "firewall_remote_admin", "firewall_review"),
    ),
    Audit(
        "Test 2: Patching, by authenticated vulnerability scan of sampled devices",
        "A sample of computers, servers and cloud servers is scanned while signed in. Any high-risk or critical "
        "update older than 14 days, or an unsupported operating system or application, fails the test.",
        "The Windows release of each managed computer and whether it's still supported, so unsupported "
        "machines can be replaced before the sample is picked. Patch dates aren't visible.",
        ("supported_os", "os_updates_14_days", "app_updates_14_days"),
    ),
    Audit(
        "Test 3: Malware protection on sampled devices",
        "On each sampled device the assessor checks that anti-malware is installed, updating and scanning "
        "(or that only approved apps can install), then sends test files by email and downloads them in the "
        "browser: they must be blocked.",
        "Whether managed devices meet their Intune compliance policy, if that policy requires anti-malware. "
        "The email test lands in Exchange Online: Exchange Online Protection or the mail gateway must block it.",
        ("anti_malware",),
    ),
    Audit(
        "Test 4: Multi-factor authentication on cloud services",
        "Users and administrators of sampled devices sign in to each cloud service from an untrusted browser "
        "session. A Pass needs an MFA prompt before access, for every authentication service in use.",
        "Exactly this for Microsoft 365: the Conditional Access policies (or Security Defaults) that require "
        "MFA, every account not covered, and the admins without an MFA method. Fix every gap first.",
        ("mfa_users", "mfa_admins"),
    ),
    Audit(
        "Test 5: Account separation",
        "On each sampled device a standard user tries to run an administrative task. A Pass needs a prompt for "
        "separate administrator credentials.",
        "Whether Microsoft 365 admin roles sit on separate accounts rather than the ones people use every day. "
        "Local administrator rights on the devices themselves aren't visible.",
        ("separate_admins",),
    ),
)

CYBER_ESSENTIALS_PLUS = register(
    Scheme(
        "cyber-essentials-plus",
        "Cyber Essentials Plus",
        "CE Plus",
        "NCSC and IASME",
        "uk",
        "The same five controls as Cyber Essentials, audited: a certification body tests a sample of devices "
        "and every cloud service against the locked self-assessment. Use the verdicts to clear every gap "
        "before the assessor's visit; the self-assessment can't be changed once testing starts.",
        THEMES,
        version=VERSION + "; test cases from the Cyber Essentials Plus Test Specification (v3.2, April 2025; "
        "v3.3 applies to assessment accounts created from 27 April 2026)",
        source=SOURCE + " Test cases paraphrased from the NCSC's published Cyber Essentials Plus Test Specification; "
        "the certification body's own specification is the one that counts.",
        source_url="https://www.ncsc.gov.uk/cyberessentials/overview",
        strict=True,
        questions="cyber-essentials",
        audit=CE_PLUS_TESTS,
        notes=NOTES
        + (
            "Cyber Essentials Plus is taken within three months of the Cyber Essentials self-assessment, on the "
            "same scope. From v3.3 the self-assessment is locked before the first device is tested, and a "
            "difference between it and what the assessor finds is recorded as a finding.",
            "The assessor samples devices from the inventory in A2.6 to A2.8: make sure it matches what Intune "
            "and Entra ID hold, since a device missing from the inventory can still be sampled.",
        ),
        renewal="12 months",
    )
)
