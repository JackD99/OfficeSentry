"""The ASD Essential Eight, judged at Maturity Level One where Microsoft 365 can see it.

The Australian Signals Directorate's eight mitigation strategies (application control, patching applications,
Office macro settings, user application hardening, restricting admin privileges, patching operating systems,
MFA and regular backups) are assessed at maturity levels one to three. Microsoft 365 data speaks to three of
them (admin privileges, operating system support and MFA); the rest are settings in Intune, Group Policy or
other tools Office Sentry doesn't read, so they carry text to complete. The strategies are ASD's; the
requirements are paraphrased.

``strict``: a maturity level is met only when every requirement at that level is.
"""

from __future__ import annotations

from . import evidence_topics  # noqa: F401  (the topics the controls name must be registered first)
from .schemes import Control, Scheme, Theme, figures_admins, figures_devices, figures_mfa_users, register

OUTSIDE = "All of this. Answer from Intune, Group Policy or the tool that sets it."

PREVENT = Theme(
    "prevent",
    "Prevent malware running",
    "Only approved programs run, applications are patched, Office macros are controlled and applications hardened.",
    (
        Control(
            "application_control",
            "1",
            "Application control",
            "Only approved programs, scripts and installers can run on workstations, including from user folders.",
            topic="application_control",
            scope=OUTSIDE,
        ),
        Control(
            "patch_applications",
            "2",
            "Patch applications",
            "Internet-facing services, browsers, Office, PDF software and email clients are patched promptly, and "
            "unsupported applications are removed.",
            topic="app_updates",
            scope=OUTSIDE,
        ),
        Control(
            "office_macros",
            "3",
            "Configure Microsoft Office macro settings",
            "Macros are blocked for users who don't need them, macros from the internet are blocked, and users "
            "can't change the settings.",
            topic="office_macros",
            scope=OUTSIDE,
        ),
        Control(
            "app_hardening",
            "4",
            "User application hardening",
            "Browsers don't process Java or ads from the internet, and users can't change browser security settings.",
            topic="app_hardening",
            scope=OUTSIDE,
        ),
    ),
)

LIMIT = Theme(
    "limit",
    "Limit the extent of incidents",
    "Admin privileges are few and separate, operating systems are patched, and everyone uses MFA.",
    (
        Control(
            "restrict_admin",
            "5",
            "Restrict administrative privileges",
            "Privileged access is validated when first asked for, limited to what's needed, held by separate "
            "accounts that don't read email or browse the web, and removed when it's no longer needed.",
            rules=("global_admin_count", "guest_admins", "service_principal_admins", "cloud_only_admins"),
            topic="separate_admins",
            report="admins",
            figures=figures_admins,
            scope="Judged from Microsoft 365 and Entra ID admin roles. Add local and server admins.",
        ),
        Control(
            "patch_os",
            "6",
            "Patch operating systems",
            "Operating systems are patched promptly and versions no longer supported by the vendor are replaced.",
            rules=("supported_windows",),
            topic="devices_patched",
            support=("device_checkin",),
            report="devices",
            figures=figures_devices,
            scope="Judged from the Windows versions Intune reports. Add servers and devices it doesn't manage.",
        ),
        Control(
            "mfa",
            "7",
            "Multi-factor authentication",
            "Users authenticate with MFA to online services holding sensitive data, and admins use phishing-resistant "
            "MFA at higher maturity levels.",
            rules=("mfa_enforced", "admins_ca_mfa", "legacy_auth_blocked"),
            topic="mfa_remote",
            support=("admin_phishing_resistant", "phone_only_mfa"),
            report="mfa",
            figures=figures_mfa_users,
            scope="Judged for Microsoft 365 sign-ins. Add other online services.",
        ),
    ),
)

RECOVER = Theme(
    "recover",
    "Recover data and availability",
    "Backups are taken, kept and tested, and can't be changed or deleted by ordinary accounts.",
    (
        Control(
            "backups",
            "8",
            "Regular backups",
            "Backups of data, applications and settings are made, kept, protected from change and deletion, and "
            "restores are tested.",
            topic="backups",
            scope="Microsoft 365 doesn't back itself up for this purpose; describe the backup of Microsoft 365 too.",
        ),
    ),
)

ESSENTIAL_EIGHT = register(
    Scheme(
        "essential-eight",
        "Essential Eight (Australia)",
        "Essential Eight",
        "Australian Signals Directorate",
        "international",
        "The eight strategies at Maturity Level One: admin privileges, operating system patching and MFA judged from "
        "Microsoft 365, the rest as text to complete from Intune or Group Policy.",
        (PREVENT, LIMIT, RECOVER),
        "ASD Essential Eight Maturity Model, Maturity Level One",
        "Strategies are ASD's, numbered in their usual order; requirements are paraphrased. Check the current "
        "maturity model on cyber.gov.au before an assessment.",
        "https://www.cyber.gov.au/resources-business-and-government/essential-cybersecurity/essential-eight",
        strict=True,
        goal="reaching Maturity Level One",
        order=50,
        notes=(
            "Maturity Level One is met only when all eight strategies meet it. Higher levels add requirements (such "
            "as phishing-resistant MFA and 48-hour patching) this pack doesn't judge.",
        ),
    )
)
