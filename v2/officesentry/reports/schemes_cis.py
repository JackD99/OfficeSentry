"""The CIS Microsoft 365 Foundations Benchmark: the recommendations Office Sentry's checks read, setting by setting.

The benchmark is a list of settings for a Microsoft 365 tenant, each with how to audit and fix it. Most of
what Office Sentry already checks maps straight onto one, so this pack is mostly judged from the data; the
rest (Defender and Purview settings Office Sentry doesn't read) are left out rather than guessed, and the
page says so. Recommendations are matched by title, since CIS renumbers them between versions, and their
wording is paraphrased. Not a certification, so a gap on a few objects is Partly.
"""

from __future__ import annotations

from . import evidence_topics  # noqa: F401  (the topics the controls name must be registered first)
from .schemes import Control, Scheme, Theme, figures_admins, figures_mfa_users, register

ADMIN_CENTRE = Theme(
    "admin_centre",
    "Microsoft 365 admin centre",
    "Few, cloud-only Global Administrators, no shared mailboxes anyone can sign in to, and a way to get apps approved.",
    (
        Control(
            "admin_count",
            "",
            "Two to four Global Administrators",
            "Between two and four accounts hold the Global Administrator role.",
            rules=("global_admin_count",),
            report="admins",
            figures=figures_admins,
            scope="Judged from Entra ID role assignments, including through groups.",
        ),
        Control(
            "cloud_only_admins",
            "",
            "Administrative accounts are cloud-only",
            "Accounts with admin roles aren't synchronised from on-premises Active Directory.",
            rules=("cloud_only_admins",),
            report="admins",
            scope="Judged from Entra ID admin role holders.",
        ),
        Control(
            "shared_mailbox_signin",
            "",
            "Sign-in to shared mailboxes is blocked",
            "The accounts behind shared mailboxes can't be signed in to.",
            rules=("shared_mailbox_signin",),
            report="shared-mailboxes",
            scope="Judged from Exchange Online and Entra ID.",
        ),
        Control(
            "guest_admins",
            "",
            "No guests or apps with admin roles",
            "Admin roles are held by internal people, not guests, and apps holding roles are reviewed.",
            rules=("guest_admins", "service_principal_admins"),
            report="admins",
            scope="Judged from Entra ID role assignments.",
        ),
    ),
)

IDENTITY = Theme(
    "identity",
    "Microsoft Entra ID",
    "MFA for everyone with Conditional Access, phishing-resistant MFA for admins, legacy authentication blocked, "
    "no user consent to apps, and users can't register apps, create tenants or join by email.",
    (
        Control(
            "mfa_all",
            "",
            "MFA required for all users",
            "A Conditional Access policy requires multi-factor authentication for every user.",
            rules=("mfa_enforced",),
            support=("users_mfa_registered",),
            report="mfa",
            figures=figures_mfa_users,
            scope="Judged from Conditional Access and Security Defaults.",
        ),
        Control(
            "mfa_admins",
            "",
            "MFA required for administrative roles",
            "A Conditional Access policy requires multi-factor authentication for every admin role.",
            rules=("admins_ca_mfa", "admin_mfa"),
            report="admins",
            figures=figures_admins,
            scope="Judged from Conditional Access and Entra ID admin roles.",
        ),
        Control(
            "phishing_resistant",
            "",
            "Phishing-resistant MFA for administrators",
            "Administrators sign in with a phishing-resistant method (passkey, FIDO2 key or Windows Hello for "
            "Business).",
            rules=("admin_phishing_resistant",),
            report="auth-methods",
            scope="Judged from the methods each admin has registered.",
        ),
        Control(
            "weak_methods",
            "",
            "Weak authentication methods not relied on",
            "Text messages and voice calls aren't anyone's only second factor.",
            rules=("phone_only_mfa",),
            report="auth-methods",
            scope="Judged from Entra ID authentication methods.",
        ),
        Control(
            "legacy_auth",
            "",
            "Legacy authentication blocked",
            "A Conditional Access policy blocks legacy authentication.",
            rules=("legacy_auth_blocked",),
            report="legacy-auth",
            scope="Judged from Conditional Access.",
        ),
        Control(
            "user_consent",
            "",
            "Users can't consent to apps",
            "User consent to applications is turned off (or limited to verified publishers and low-risk permissions).",
            rules=("user_consent",),
            report="apps",
            scope="Judged from Entra ID consent settings.",
        ),
        Control(
            "admin_consent_workflow",
            "",
            "Admin consent workflow on",
            "Users can ask for an app to be approved, and admins review the request.",
            rules=("admin_consent_workflow",),
            report="apps",
            scope="Judged from Entra ID consent settings.",
        ),
        Control(
            "app_registration",
            "",
            "Users can't register applications",
            "Only admins can register applications.",
            rules=("app_registration",),
            report="tenant-settings",
            scope="Judged from Entra ID user settings.",
        ),
        Control(
            "tenant_creation",
            "",
            "Non-admins can't create tenants",
            "Users who aren't admins can't create new tenants.",
            rules=("tenant_creation",),
            report="tenant-settings",
            scope="Judged from Entra ID user settings.",
        ),
        Control(
            "guest_access",
            "",
            "Guest access to the directory restricted",
            "Guests have limited access to directory objects, and only admins and the Guest Inviter role can invite.",
            rules=("guest_access_level", "guest_invites"),
            report="guests",
            scope="Judged from Entra ID external collaboration settings.",
        ),
        Control(
            "email_join",
            "",
            "No joining the tenant by email verification",
            "People can't join the tenant just by verifying an email address.",
            rules=("email_verified_join",),
            report="tenant-settings",
            scope="Judged from Entra ID authorisation settings.",
        ),
        Control(
            "group_creation",
            "",
            "Group creation limited",
            "Only chosen people can create Microsoft 365 groups and teams.",
            rules=("group_creation",),
            report="groups",
            scope="Judged from Entra ID group settings.",
        ),
        Control(
            "risky_users",
            "",
            "Risky users and sign-ins dealt with",
            "Entra ID Protection risk is acted on: no account left at risk, no risky sign-in left open.",
            rules=("risky_users", "risky_sign_ins"),
            report="risky-users",
            scope="Judged from Entra ID Protection (Entra ID P2 for the full detections).",
        ),
        Control(
            "password_hash_sync",
            "",
            "Password hash sync on (hybrid)",
            "Where accounts sync from Active Directory, password hash sync is on so leaked credentials are detected.",
            rules=("password_hash_sync",),
            report="hybrid",
            scope="Judged from Entra Connect; not applicable to cloud-only tenants.",
        ),
    ),
)

EXCHANGE = Theme(
    "exchange",
    "Exchange Online",
    "Auditing on, no forwarding outside, SMTP AUTH off, and every domain with SPF, DKIM and DMARC.",
    (
        Control(
            "mailbox_auditing",
            "",
            "Mailbox auditing on",
            "Mailbox auditing is on for every user mailbox.",
            rules=("mailbox_auditing",),
            report="tenant-settings",
            scope="Judged from Exchange Online.",
        ),
        Control(
            "forwarding",
            "",
            "All forms of mail forwarding blocked",
            "Automatic forwarding to outside addresses is blocked by policy, and no mailbox or rule forwards out.",
            rules=("auto_forward_policy", "external_forwarding"),
            report="mailboxes",
            scope="Judged from the outbound spam policy, mailbox forwarding and inbox rules.",
        ),
        Control(
            "inbox_rules",
            "",
            "No inbox rules hiding mail",
            "Inbox rules that delete, hide or redirect mail are found and reviewed.",
            rules=("suspicious_inbox_rules",),
            report="inbox-rules",
            scope="Judged from Exchange Online inbox rules.",
        ),
        Control(
            "smtp_auth",
            "",
            "SMTP AUTH disabled",
            "Authenticated SMTP is turned off for the organisation.",
            rules=("smtp_auth",),
            report="legacy-auth",
            scope="Judged from Exchange Online transport settings.",
        ),
        Control(
            "spf",
            "",
            "SPF records published",
            "Every mail domain publishes an SPF record.",
            rules=("spf_record",),
            report="domains",
            scope="Judged from public DNS.",
        ),
        Control(
            "dkim",
            "",
            "DKIM enabled",
            "DKIM signing is on for every mail domain.",
            rules=("dkim_enabled",),
            report="domains",
            scope="Judged from Exchange Online and public DNS.",
        ),
        Control(
            "dmarc",
            "",
            "DMARC published and enforced",
            "Every domain publishes a DMARC record with a quarantine or reject policy.",
            rules=("dmarc_policy",),
            report="domains",
            scope="Judged from public DNS.",
        ),
    ),
)

SHAREPOINT = Theme(
    "sharepoint",
    "SharePoint, OneDrive and Teams",
    "External sharing is limited, there are no open anyone links, and groups and teams have owners.",
    (
        Control(
            "external_sharing",
            "",
            "External sharing restricted",
            "SharePoint and OneDrive sharing doesn't allow anyone links.",
            rules=("sharepoint_sharing",),
            report="shared-files",
            scope="Judged from SharePoint tenant settings.",
        ),
        Control(
            "anyone_links",
            "",
            "No anyone links in use",
            "No files or folders are shared with anyone who has the link.",
            rules=("anyone_links",),
            report="shared-files",
            scope="Judged from SharePoint and OneDrive sharing links.",
        ),
        Control(
            "guest_review",
            "",
            "Guest users reviewed",
            "Guest accounts are reviewed, and invitations nobody accepted are removed.",
            rules=("pending_guests",),
            topic="guest_review",
            report="guests",
            scope="Judged from Entra ID guests and their invitation state.",
        ),
        Control(
            "group_owners",
            "",
            "Every group and team has an owner",
            "Microsoft 365 groups and teams have owners who answer for their membership and content.",
            rules=("ownerless_groups", "group_expiration"),
            report="groups",
            scope="Judged from Entra ID groups.",
        ),
    ),
)

DEVICES = Theme(
    "devices",
    "Intune and devices",
    "Devices are enrolled, compliant, encrypted and on supported versions.",
    (
        Control(
            "enrolled",
            "",
            "Devices enrolled and compliant",
            "Company computers are managed by Intune, check in, and meet the compliance policies.",
            rules=("intune_enrolled", "device_compliance", "device_checkin"),
            report="devices",
            scope="Judged from Intune and Entra ID.",
        ),
        Control(
            "encrypted",
            "",
            "Disks encrypted",
            "Company computers are encrypted.",
            rules=("device_encryption",),
            report="devices",
            scope="Judged from Intune.",
        ),
        Control(
            "supported",
            "",
            "Supported operating systems",
            "Windows computers run a version that still gets security updates.",
            rules=("supported_windows",),
            report="devices",
            scope="Judged from the Windows versions Intune reports.",
        ),
    ),
)

CIS_M365 = register(
    Scheme(
        "cis-m365",
        "CIS Microsoft 365 Foundations Benchmark",
        "CIS M365",
        "Center for Internet Security",
        "international",
        "The benchmark's recommendations that Office Sentry reads, judged setting by setting, with the fix for each. "
        "Use it as the evidence for a CIS-aligned audit or a client's hardening baseline.",
        (ADMIN_CENTRE, IDENTITY, EXCHANGE, SHAREPOINT, DEVICES),
        "CIS Microsoft 365 Foundations Benchmark, recommendations matched by title",
        "Recommendations paraphrased from the CIS Microsoft 365 Foundations Benchmark. CIS renumbers them between "
        "versions, so match each one by its title in the version you're auditing against. Defender, Purview and "
        "Teams meeting settings Office Sentry doesn't read are left out rather than guessed.",
        "https://www.cisecurity.org/benchmark/microsoft_365",
        order=20,
        notes=(
            "This is a subset of the benchmark: the recommendations Office Sentry can judge from read-only data. A "
            "Pass is evidence for that recommendation; the rest of the benchmark still needs checking in the admin "
            "centres.",
        ),
    )
)
