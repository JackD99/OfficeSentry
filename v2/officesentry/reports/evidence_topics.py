"""More topics questionnaires ask about, beyond the insurance pack's original sixteen.

Some are answered from the stored snapshot (the device and cloud service lists
Cyber Essentials asks for, how mail is filtered, admin access, sign-in risk).
The rest are outside what Microsoft 365 can show (firewalls, patch timing,
anti-malware, plans and processes): each one says so and gives the engineer a
fill-in text with the [parts] to complete. Nothing here is guessed.

The texts are worded the way forms expect, third person, about "the
organisation". Imported at the bottom of ``evidence_answers`` (which the
helpers below come from); ``evidence_answers.QUESTIONS`` is both lists.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from .. import devices, dnscheck, hybrid
from ..checks.identity import _privileged_users
from ..licences import sku_name
from ..snapshot import TenantSnapshot
from ..text import plural
from .evidence_base import (
    NOT_KNOWN,
    PARTLY,
    YES,
    Question,
    Source,
    _checks,
    _from_checks,
    _mfa_everyone,
    _sentence,
    gap,
    lead,
)

LIST_LIMIT = 20  # names listed in an answer before "and N more"
COUNTRIES = {
    "GB": "the United Kingdom",
    "IE": "Ireland",
    "US": "the United States",
    "AU": "Australia",
    "NZ": "New Zealand",
    "CA": "Canada",
    "DE": "Germany",
    "FR": "France",
    "NL": "the Netherlands",
}
# Inbound mail gateways recognisable from MX hosts, so the answer can name the product.
GATEWAYS = (
    ("mimecast.com", "Mimecast"),
    ("pphosted.com", "Proofpoint"),
    ("ppe-hosted.com", "Proofpoint"),
    ("messagelabs.com", "Symantec Email Security.cloud"),
    ("barracudanetworks.com", "Barracuda"),
    ("hornetsecurity.com", "Hornetsecurity"),
    ("sophos.com", "Sophos"),
    ("mailcontrol.com", "Forcepoint"),
    ("iphmx.com", "Cisco Secure Email"),
    ("trendmicro", "Trend Micro"),
    ("mailguard", "MailGuard"),
    ("spamtitan.com", "SpamTitan"),
    ("libraesva", "Libraesva"),
)


def _listed(names: list[str]) -> str:
    names = sorted(dict.fromkeys(n for n in names if n), key=str.lower)
    more = len(names) - LIST_LIMIT
    return ", ".join(names[:LIST_LIMIT]) + (f" and {more} more" if more > 0 else "")


# -- Answered from the data ----------------------------------------------------------------


def _admin_count(snap: TenantSnapshot, results):
    admins = _privileged_users(snap)
    if admins is None:
        return NOT_KNOWN, "Admin role data hasn't been collected yet."
    people = {
        a["principalId"]: a.get("userPrincipalName") or a.get("displayName") or a["principalId"]
        for a in admins
        if a.get("accountEnabled") is not False
    }
    if not people:
        return NOT_KNOWN, "No active admin role holders were found."
    ga = {a["principalId"] for a in admins if a.get("roleName") == "Global Administrator"} & set(people)
    answer, why = _from_checks(results, ("global_admin_count", "guest_admins", "service_principal_admins"))
    listed = f"{plural(len(people), 'account')} {'holds' if len(people) == 1 else 'hold'} privileged admin roles"
    if ga:
        listed += f", {len(ga)} of them Global Administrator ({_listed([people[p] for p in ga])})"
    return (YES if answer == NOT_KNOWN else answer), f"{listed}. {why}".strip()


NOT_COLLECTED = "Nothing has been collected from Microsoft 365 yet."


def _password_policy(snap: TenantSnapshot, results):
    if not snap.has("tenant_profile"):
        return NOT_KNOWN, NOT_COLLECTED
    parts = [
        "Microsoft Entra ID requires passwords of at least 8 characters, blocks commonly used and known "
        "compromised passwords (Microsoft's global banned password list) and locks out repeated failed "
        "sign-in attempts (smart lockout). These are Microsoft defaults for cloud accounts."
    ]
    mfa, _why = _mfa_everyone(snap, results)
    if mfa == YES:
        parts.append(
            "Multi-factor authentication is enforced as well (see the MFA answer), so a guessed password "
            "alone can't be used to sign in."
        )
    elif mfa == PARTLY:
        parts.append("Multi-factor authentication is enforced for most users as well (see the MFA answer).")
    sync = hybrid.sync_status(snap)
    if sync and sync.get("enabled"):
        parts.append(
            "Accounts are synchronised from on-premises Active Directory, so its password policy also applies."
        )
    return YES, " ".join(parts)


def _email_filtering(snap: TenantSnapshot, results):
    rows = snap.objects("domain_health", "domain")
    if rows is None:
        return NOT_KNOWN, "Email domains haven't been checked yet."
    mail = [h for h in (dnscheck.DomainHealth.from_dict(r) for r in rows if isinstance(r, dict)) if h.sends_mail]
    if not mail:
        return NOT_KNOWN, "None of the tenant's domains receives email."
    direct = [h.domain for h in mail if h.microsoft_365]
    via: dict[str, list[str]] = {}
    for h in mail:
        if not h.microsoft_365:
            host = next((m for m in h.mx if m), "")
            product = next((name for part, name in GATEWAYS if part in host.lower()), "")
            via.setdefault(product or f"an email security gateway ({host})", []).append(h.domain)
    parts = []
    if direct:
        parts.append(
            f"Incoming email for {_listed(direct)} is delivered to Microsoft 365 and filtered by Exchange "
            "Online Protection (anti-spam, anti-phishing and anti-malware)."
        )
    for gateway, names in via.items():
        parts.append(f"Incoming email for {_listed(names)} passes through {gateway} before it reaches Microsoft 365.")
    caps = snap.capabilities
    if caps.get("defender_o365_p1"):
        parts.append("Microsoft Defender for Office 365 is licensed, adding Safe Links and Safe Attachments.")
    return YES, " ".join(parts)


def _data_location(snap: TenantSnapshot, results):
    orgs = snap.objects("tenant_profile", "organization")
    if not orgs:
        return NOT_KNOWN, "The tenant's organisation details haven't been collected yet."
    code = (orgs[0].get("countryLetterCode") or "").upper()
    where = f" The Microsoft 365 tenant is registered in {COUNTRIES.get(code, code)}." if code else ""
    return YES, ("Email, files and Teams data are held in Microsoft 365, in Microsoft's datacentres." + where)


def _encryption(snap: TenantSnapshot, results):
    if not snap.has("tenant_profile"):
        return NOT_KNOWN, NOT_COLLECTED
    parts = [
        "Data in Microsoft 365 is encrypted at rest by Microsoft (BitLocker and per-file service encryption) and "
        "in transit using TLS."
    ]
    device = results.get("device_encryption")
    answer = YES
    if device and device.status == "pass":
        parts.append("Company computers managed in Microsoft Intune are encrypted too (BitLocker or FileVault).")
    elif device and device.status == "fail":
        answer = PARTLY
        parts.append(f"Not every company computer is encrypted: {_sentence(device.summary)}")
    return answer, " ".join(parts)


def _os_name(d: dict, today) -> str:
    os_name = d.get("operatingSystem") or "an unknown operating system"
    version = d.get("osVersion") or d.get("operatingSystemVersion") or ""
    if devices.is_windows(d):
        release = devices.windows_support(version, today).release
        return release if release != "Windows" else f"Windows {version}".strip()
    if os_name.lower().startswith(("mac", "ios", "ipados")):
        os_name = {"macos": "macOS", "mac os x": "macOS", "ios": "iOS", "ipados": "iPadOS"}.get(
            os_name.lower(), os_name
        )
    return f"{os_name} {version}".strip()


def _inventory(snap: TenantSnapshot, want, what: str):
    managed = devices.objects(snap, "managed_device")
    entra = devices.objects(snap, "entra_device")
    if managed is None and entra is None:
        return NOT_KNOWN, "Devices haven't been collected yet."
    now = datetime.now(UTC)
    if managed:
        rows, where = [d for d in managed if want(d)], "managed in Microsoft Intune"
    else:
        rows = [
            d
            for d in entra or []
            if want(d)
            and d.get("accountEnabled") is not False
            and (devices.days_since(d.get("approximateLastSignInDateTime"), now) or 0) <= devices.STALE_DAYS
        ]
        where = "registered in Microsoft Entra ID and seen in the last 90 days"
    if not rows:
        return NOT_KNOWN, f"No {what} are {where}."
    counted = Counter(((d.get("manufacturer") or "").strip(), _os_name(d, now.date())) for d in rows)
    lines = [
        f"{plural(n, (maker + ' ' + what[:-1]).strip())} running {os_name}"
        for (maker, os_name), n in sorted(counted.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return YES, f"{plural(len(rows), what[:-1])} {where}: {'; '.join(lines)}."


def _computers(snap: TenantSnapshot, results):
    return _inventory(snap, devices.is_computer, "computers")


def _mobiles(snap: TenantSnapshot, results):
    return _inventory(
        snap, lambda d: (d.get("operatingSystem") or "").lower().startswith(("ios", "ipados", "android")), "devices"
    )


def _cloud_services(snap: TenantSnapshot, results):
    skus = snap.objects("tenant_profile", "subscribed_sku")
    if skus is None:
        return NOT_KNOWN, "Subscriptions haven't been collected yet."
    plans = [
        sku_name(s.get("skuPartNumber"))
        for s in skus
        if (s.get("prepaidUnits") or {}).get("enabled", 0) > 0 and s.get("appliesTo") == "User"
    ]
    parts = [f"Microsoft 365 ({_listed(plans)})." if plans else "Microsoft 365."]
    sps = snap.objects("app_consents", "service_principal")
    if sps:
        own = snap.own_app_ids
        apps = [
            sp.get("displayName")
            for sp in sps
            if not sp.get("microsoft")
            and not sp.get("ownTenant")
            and sp.get("type") == "Application"
            and sp.get("accountEnabled") is not False
            and (sp.get("appId") or "").lower() not in own
        ]
        if apps:
            parts.append(f"Cloud services connected to Microsoft 365 sign-in: {_listed(apps)}.")
    return YES, " ".join(parts)


# -- The topics ----------------------------------------------------------------------------

DATA = (
    Question(
        "admin_count",
        "Is administrator access limited to a few named people, and tracked?",
        _admin_count,
        Source("Admins and roles", "admins"),
        lead(
            "Administrator access in Microsoft 365 is limited to a small number of named accounts, and "
            "administrator role assignments are tracked.",
            "Administrator role assignments in Microsoft 365 are tracked, with gaps.",
            "Administrator access in Microsoft 365 is not limited to a small number of named accounts.",
        ),
        "Add administrators of servers, firewalls and other systems, and say how the list is kept.",
        collectors=("admin_roles",),
    ),
    Question(
        "admins_phishing_resistant",
        "Do administrators sign in with phishing-resistant MFA?",
        _checks("admin_phishing_resistant"),
        Source("Passwords and sign-in methods", "auth-methods"),
        lead(
            "Administrator accounts sign in with phishing-resistant methods (passkeys, FIDO2 security keys or "
            "Windows Hello for Business).",
            "Some administrator accounts sign in with phishing-resistant methods.",
            "Administrator accounts don't all use phishing-resistant sign-in methods yet.",
        ),
        collectors=("admin_roles", "mfa_registration"),
    ),
    Question(
        "mfa_methods",
        "Are weaker MFA methods, such as text messages, avoided?",
        _checks("phone_only_mfa"),
        Source("Passwords and sign-in methods", "auth-methods"),
        lead(
            "No user relies on a text message or phone call as their only second factor; people use the "
            "Microsoft Authenticator app or stronger methods.",
            "Most users have a stronger second factor than a text message or phone call.",
            "Some users rely on a text message or phone call as their only second factor.",
        ),
        collectors=("mfa_registration",),
    ),
    Question(
        "shared_accounts",
        "Does everyone sign in with their own unique account?",
        _checks("shared_mailbox_signin"),
        Source("Shared mailboxes", "shared-mailboxes"),
        lead(
            "Each person signs in to Microsoft 365 with their own named account. Shared mailboxes can't be "
            "signed in to directly; people reach them through their own accounts.",
            "People sign in with their own named accounts, but some shared mailboxes can be signed in to.",
            "Shared mailboxes can be signed in to directly, so not every sign-in is tied to a person.",
        ),
        "Check devices, line-of-business apps and other systems for shared logins.",
        collectors=("mailboxes", "users"),
    ),
    Question(
        "password_policy",
        "How are passwords protected against guessing and brute-force attacks?",
        _password_policy,
        Source("Passwords and sign-in methods", "auth-methods"),
        beyond="The password rules are Microsoft's defaults, not read from the tenant. Add other systems that "
        "use passwords (devices, servers, other cloud services).",
        collectors=("tenant_profile", "conditional_access", "directory_sync"),
        kind="text",
    ),
    Question(
        "risk_detection",
        "Are suspicious sign-ins detected and investigated?",
        _checks("risky_sign_ins", "risky_users"),
        Source("Sign-in activity", "sign-ins"),
        lead(
            "Sign-ins are checked for risk (unfamiliar locations, leaked credentials, impossible travel) by "
            "Microsoft Entra ID Protection.",
            "Sign-ins are checked for risk by Microsoft Entra ID Protection, and some risks are still open.",
            "Risky sign-ins detected by Microsoft Entra ID Protection haven't all been dealt with.",
        ),
        "Say who reviews alerts and how quickly.",
        template="Sign-ins are monitored for suspicious activity by [tool or service], and alerts are "
        "reviewed by [team or provider] [within one working day].",
        collectors=("sign_ins", "risky_users"),
    ),
    Question(
        "email_filtering",
        "Is incoming email filtered for spam, phishing and malware?",
        _email_filtering,
        Source("Email domains", "domains"),
        lead("Incoming email is filtered for spam, phishing and malware."),
        "Office Sentry sees the mail routing and licences, not the filtering policies. Confirm Safe Links and "
        "Safe Attachments (or the gateway's equivalent) apply to every user.",
        template="Incoming email is filtered for spam, phishing and malware by [email security product].",
        collectors=("domain_health", "tenant_profile"),
    ),
    Question(
        "data_location",
        "Where is the organisation's data stored?",
        _data_location,
        beyond="Confirm the data location in the Microsoft 365 admin centre (Settings > Org settings > "
        "Organization profile > Data location), and add data held anywhere else.",
        collectors=("tenant_profile",),
        kind="text",
    ),
    Question(
        "encryption",
        "Is data encrypted at rest and in transit?",
        _encryption,
        Source("Devices: managed devices", "devices", "Managed devices"),
        beyond="The Microsoft 365 encryption is Microsoft's own, not read from the tenant. Add servers, "
        "backups and other systems.",
        collectors=("tenant_profile", "devices"),
    ),
    Question(
        "device_inventory",
        "Which laptops, desktops and operating systems are in use?",
        _computers,
        Source("Devices", "devices"),
        beyond="Microsoft 365 doesn't report the Windows edition (Pro, Enterprise): add it. Add any computers "
        "not managed by Intune or joined to Entra ID, and servers.",
        template="[Number] [make] laptops and desktops running [operating system, edition and version].",
        collectors=("devices",),
        kind="text",
    ),
    Question(
        "mobile_inventory",
        "Which tablets and mobile phones are in use?",
        _mobiles,
        Source("Devices", "devices"),
        beyond="Add personal phones and tablets (BYOD) that reach work email or files but aren't enrolled.",
        template="[Number] [make] phones and tablets running [operating system and version].",
        collectors=("devices",),
        kind="text",
    ),
    Question(
        "cloud_services",
        "Which cloud services does the organisation use?",
        _cloud_services,
        Source("Apps and consents", "apps"),
        beyond="Add cloud services people sign in to with separate accounts (accounting, payroll, CRM, file "
        "transfer, social media); Microsoft 365 can't see them.",
        collectors=("tenant_profile", "app_consents"),
        kind="text",
    ),
)

M365 = "Microsoft 365 can't show this."

GAPS = (
    gap(
        "remote_access",
        "Is MFA required for all remote access to the network (VPN, remote desktop, remote support tools)?",
        f"{M365} It sees Microsoft 365 sign-ins (see the MFA answer), not VPNs or remote desktop.",
        "Remote access to the network is only possible through [VPN or remote access product], which requires "
        "multi-factor authentication. Remote support tools ([product]) also require multi-factor authentication.",
    ),
    gap(
        "rdp_exposed",
        "Is remote desktop (RDP) closed to the internet?",
        f"{M365} It's a firewall setting.",
        "Remote desktop (RDP) is not reachable from the internet. Remote connections go through [VPN or remote "
        "desktop gateway] with multi-factor authentication. This was last confirmed on [date] by [external scan or "
        "firewall review].",
    ),
    gap(
        "edr",
        "Is endpoint detection and response (EDR) installed and monitored on all computers and servers?",
        f"{M365} Office Sentry doesn't read Microsoft Defender for Endpoint or other EDR consoles.",
        "[EDR product, for example Microsoft Defender for Endpoint] is installed on all [number] computers and "
        "servers. Alerts are monitored by [team or security provider] [24/7 or in business hours].",
    ),
    gap(
        "anti_malware",
        "Are all computers, tablets and phones protected from malware?",
        f"{M365} Office Sentry doesn't read anti-malware settings.",
        "All desktops and laptops run [anti-malware product, for example Microsoft Defender Antivirus], which "
        "updates automatically, scans files when they're opened and blocks malicious websites. Tablets and phones "
        "only install apps from the official app stores.",
    ),
    gap(
        "patching_14_days",
        "Are critical and high-risk security updates installed within 14 days of release?",
        f"{M365} Intune shows which Windows version each computer runs (see the supported systems answer), not "
        "when updates were installed.",
        "Operating system updates install automatically through [Windows Update for Business, Intune update rings or "
        "the RMM tool]. Critical and high-risk updates, including router and firewall firmware, are installed within "
        "14 days of release, and this is checked [weekly] by [IT provider].",
    ),
    gap(
        "software_supported",
        "Is every application supported by its vendor and licensed, with unsupported software removed?",
        f"{M365} Office Sentry doesn't see which application versions are installed.",
        "All software in use is supported by its vendor and licensed: [browser and version], [anti-malware "
        "product and version], [email application and version] and [office applications and version]. Software "
        "that is no longer supported has been removed[, or runs on a separate sub-set outside the scope].",
    ),
    gap(
        "app_updates",
        "Are critical and high-risk updates for applications installed within 14 days of release?",
        f"{M365} Office Sentry doesn't see which application versions are installed.",
        "Applications update automatically where they can (Microsoft 365 Apps, web browsers and [other software]). "
        "Updates that can't install automatically are applied within 14 days of release by [process or tool].",
    ),
    gap(
        "incident_response",
        "Is there a documented, tested incident response plan?",
        M365,
        "A documented incident response plan sets out roles, contacts and the steps to contain and recover from a "
        "cyber incident, including notifying [the insurer and IT provider]. It was last reviewed on [date] and "
        "tested on [date].",
    ),
    gap(
        "bcp_dr",
        "Is there a business continuity and disaster recovery plan, and is it tested?",
        M365,
        "A business continuity and disaster recovery plan covers [key systems and services]. Recovery targets are "
        "[time to restore] to restore service and no more than [data loss window] of data loss. It was last tested on "
        "[date].",
    ),
    gap(
        "payment_verification",
        "Are requests to change bank details or make payments verified with a known contact?",
        f"{M365} It's a finance process.",
        "Requests to change supplier bank details or make urgent payments are verified by calling the requester on a "
        "number already held on file, never one given in the request. Payments over [amount] need approval by "
        "[role].",
    ),
    gap(
        "firewall_boundary",
        "Are firewalls in place between the internal network and the internet?",
        f"{M365} Firewalls aren't part of Microsoft 365.",
        "A [make and model] firewall protects the internet connection at [site]. Home and remote workers are "
        "protected by the software firewall on their company devices.",
    ),
    gap(
        "software_firewall",
        "Are software firewalls turned on for all computers, laptops and servers?",
        f"{M365} Office Sentry doesn't read firewall settings from Intune.",
        "The built-in software firewall (Microsoft Defender Firewall on Windows, the macOS firewall on Macs) is "
        "turned on for all computers and servers [and enforced by Intune policy].",
    ),
    gap(
        "firewall_passwords",
        "Have the default passwords on firewalls and routers been changed?",
        f"{M365} Firewalls aren't part of Microsoft 365.",
        "The default administrator passwords on all firewalls and routers were changed when they were installed, to "
        "[unique passwords of at least 12 characters, kept in a password manager].",
    ),
    gap(
        "firewall_review",
        "Have firewall rules been reviewed in the last 12 months?",
        f"{M365} Firewalls aren't part of Microsoft 365.",
        "Firewall rules are reviewed [every 12 months] by [IT provider], and rules that are no longer needed are "
        "removed. The last review was on [date].",
    ),
    gap(
        "firewall_inbound",
        "Do firewalls block unauthenticated inbound connections?",
        f"{M365} Firewalls aren't part of Microsoft 365.",
        "Firewalls block all inbound connections by default. [No inbound connections are allowed.] or [Each allowed "
        "inbound connection is documented, with its business need approved by (role).]",
    ),
    gap(
        "firewall_remote_admin",
        "Is the firewall's administration interface closed to the internet?",
        f"{M365} Firewalls aren't part of Microsoft 365.",
        "The firewall's administration interface can't be reached from the internet. [Or: remote administration is "
        "limited to (trusted IP addresses) and protected by multi-factor authentication.]",
    ),
    gap(
        "remote_workers",
        "How do home and remote workers connect to company data and services?",
        f"{M365} It sees where people sign in from, not how their network connects.",
        "Home and remote workers connect over [home broadband or business-provided routers] to Microsoft 365, "
        "with multi-factor authentication. [Office systems are reached through (VPN product).]",
        kind="text",
    ),
    gap(
        "software_removal",
        "Is software and services nobody uses removed or turned off?",
        M365,
        "New devices are set up from [a standard build or Windows Autopilot] without unnecessary software, and "
        "unused software and services are removed [during regular reviews by the IT provider].",
    ),
    gap(
        "default_passwords",
        "Have default passwords been changed on all devices and accounts?",
        M365,
        "Default and built-in accounts on devices are turned off or given new passwords at set-up. [Local "
        "administrator passwords are managed by Windows LAPS.]",
    ),
    gap(
        "autorun",
        "Is automatic running of files from USB drives and downloads turned off?",
        f"{M365} Office Sentry doesn't read device configuration profiles.",
        "AutoPlay and AutoRun are turned off on Windows computers [by Intune policy], and downloaded files can't run "
        "without the user's permission.",
    ),
    gap(
        "device_lock",
        "Do devices lock automatically and need a PIN, password or biometric to unlock?",
        f"{M365} Office Sentry doesn't read device configuration profiles.",
        "Computers lock after [5 to 15] minutes of inactivity and need a password, PIN or Windows Hello to unlock. "
        "Phones and tablets need a PIN of at least 6 digits or biometrics [enforced by Intune].",
    ),
    gap(
        "account_approval",
        "Are user accounts only created after an approved request?",
        M365,
        "New accounts are created only after a request from [manager or HR] is approved by [role], recorded in "
        "[ticket system].",
    ),
    gap(
        "least_privilege",
        "Do staff only have the access they need for their job?",
        M365,
        "Access to files and systems is given through role-based groups, approved by [role], and changed when "
        "people move roles. [Access is reviewed every 12 months.]",
    ),
    gap(
        "admin_process",
        "Is there a formal process for giving someone administrator access?",
        M365,
        "Administrator access is given only after approval by [role], to a separate named admin account, and is "
        "recorded in [ticket system or register].",
    ),
    gap(
        "admin_review",
        "Is administrator access reviewed regularly?",
        f"{M365} It shows who holds admin roles today (see the administrator access answer), not that a review "
        "happens.",
        "Administrator access is reviewed [quarterly] by [role] against the list of current administrators, and "
        "anyone who no longer needs it has it removed.",
    ),
    gap(
        "compromise_process",
        "Is there a process for when a password or account may be compromised?",
        M365,
        "If an account may be compromised, [IT provider] resets the password, signs the account out of every "
        "session, checks its MFA methods and inbox rules, and reviews its sign-ins. Staff report concerns to "
        "[contact].",
    ),
    gap(
        "mfa_available",
        "Do all the cloud services in use offer multi-factor authentication?",
        f"{M365} It knows about Microsoft 365, which offers MFA; other cloud services need checking.",
        "All cloud services in use offer multi-factor authentication: Microsoft 365 (through Microsoft Entra ID) "
        "and [other cloud services]. [Or list any that don't offer it.]",
    ),
    gap(
        "policies",
        "Are there documented information security policies?",
        M365,
        "Information security policies covering [acceptable use, access control, passwords, data protection and "
        "incident response] are approved by [role], reviewed every [12 months] and acknowledged by staff.",
    ),
    gap(
        "certifications",
        "Does the organisation hold security certifications, such as Cyber Essentials or ISO 27001?",
        M365,
        "The organisation holds [Cyber Essentials or Cyber Essentials Plus or ISO 27001] certification, number "
        "[certificate number], valid until [date].",
    ),
    gap(
        "staff_vetting",
        "Are staff vetted before they get access to data?",
        M365,
        "New staff go through [right-to-work, reference and DBS] checks before they get access, and sign "
        "[confidentiality agreements].",
    ),
    gap(
        "physical_security",
        "Are offices and equipment physically secured?",
        M365,
        "Offices are protected by [locked doors, access control and alarms]. [Servers and network equipment are "
        "kept in a locked room or cabinet.]",
    ),
    gap(
        "vulnerability_scanning",
        "Are systems scanned for vulnerabilities or penetration tested?",
        M365,
        "External vulnerability scans run [monthly] using [tool or provider]. [A penetration test was last carried "
        "out on (date) by (provider).]",
    ),
    # Governance and data protection: what IASME Cyber Assurance, ISO 27001 and UK GDPR ask beyond the technical
    # controls. Written for UK organisations; the ICO and UK GDPR articles are named where a form expects them.
    gap(
        "security_responsibility",
        "Who is responsible for information security and data protection?",
        M365,
        "[Name], [role], has overall responsibility for information security and data protection and reports to "
        "[the board or partners], where security is a standing agenda item. Day-to-day IT security is managed by "
        "[IT provider].",
        kind="text",
    ),
    gap(
        "risk_assessment",
        "Is there a current information security risk assessment, reviewed in the last 12 months?",
        M365,
        "An information security risk assessment covering [scope] names an owner for each risk, the agreed "
        "treatment and an action plan. It was last reviewed on [date] by [name and role] and approved by [director "
        "or partner].",
    ),
    gap(
        "asset_register",
        "Is there an up-to-date register of information and equipment, with a named owner for each?",
        f"{M365} It lists the devices Intune and Entra ID know about (see the device answers), not the full register.",
        "An asset register in [tool or spreadsheet] lists [hardware, software, cloud services and the information "
        "held], each with a named owner, a category and where it is. Devices are added when they're set up and "
        "removed when they're disposed of, and the register was last reviewed on [date].",
    ),
    gap(
        "supplier_security",
        "Are suppliers that handle the organisation's data held to security requirements?",
        f"{M365} It lists the apps connected to Microsoft 365 (see the cloud services answer), not the contracts.",
        "Suppliers and contractors that handle the organisation's data are checked before they're used, must meet "
        "[Cyber Essentials or equivalent security requirements], and are reviewed [every 12 months]. They're listed "
        "in [supplier register].",
    ),
    gap(
        "processor_contracts",
        "Is there a data processing agreement with every supplier that processes personal data?",
        M365,
        "Every supplier that processes personal data for the organisation has a written contract with the terms UK "
        "GDPR Article 28 requires: acting only on instructions, confidentiality, security, approval of "
        "sub-processors, help with people's rights, and deleting or returning the data at the end. They're recorded "
        "in [register].",
    ),
    gap(
        "ico_registration",
        "Has the organisation paid the data protection fee to the ICO?",
        M365,
        "The organisation is registered with the Information Commissioner's Office, registration number [ZA number], "
        "renewed on [date].",
        beyond="Some organisations are exempt from the fee; say so if this one is.",
    ),
    gap(
        "dpo",
        "Does the organisation need a Data Protection Officer, and who manages data protection?",
        M365,
        "[The organisation has appointed (name) as its Data Protection Officer, contactable at (email address).] or "
        "[The organisation assessed on (date) that it doesn't need a Data Protection Officer; (name, role) is "
        "responsible for data protection.]",
        kind="text",
    ),
    gap(
        "ropa",
        "Is there a record of the personal data held, why, on what lawful basis and where?",
        M365,
        "A record of processing activities lists each kind of personal data held, its purpose and lawful basis, who "
        "it's shared with, where it's stored and how long it's kept. [Role] maintains it, and it was last reviewed "
        "on [date].",
    ),
    gap(
        "privacy_notice",
        "Is there an up-to-date privacy notice?",
        M365,
        "A privacy notice at [web address] explains what personal data is collected, why and on what lawful basis, "
        "who it's shared with, how long it's kept, people's rights and how to complain. It was last reviewed on "
        "[date].",
    ),
    gap(
        "data_subject_rights",
        "How are requests from people to see, correct or delete their data handled?",
        M365,
        "Requests to see, correct or delete personal data, and complaints about how it's used, go to [contact] and "
        "are logged in [system]. They're answered within one month, and [role] checks each response before it's "
        "sent.",
        kind="text",
    ),
    gap(
        "dpia",
        "Is a data protection impact assessment done before new high-risk use of personal data?",
        M365,
        "A data protection impact assessment is carried out before any new system or process that is likely to be "
        "high risk to people, using [the ICO's template]. [Role] signs each one off, and the ICO would be consulted "
        "if a high risk remained.",
    ),
    gap(
        "breach_reporting",
        "Are personal data breaches recorded, and reported to the ICO within 72 hours when required?",
        M365,
        "Every personal data breach is recorded in a breach log with what happened, its effects and what was done. "
        "[Role] decides whether it must be reported; reportable breaches go to the ICO within 72 hours of the "
        "organisation becoming aware, and to the people affected when the risk to them is high. Lessons learned are "
        "reviewed by [role].",
    ),
    gap(
        "data_retention",
        "Is personal data deleted when it's no longer needed?",
        f"{M365} Office Sentry doesn't read Microsoft 365 retention policies.",
        "A retention schedule sets how long each kind of data is kept. Data past its retention period is deleted [by "
        "Microsoft 365 retention policies or at a review every (period)], and the data held is reviewed [every 12 "
        "months] to check it's still needed.",
    ),
    gap(
        "secure_disposal",
        "Is data securely wiped, or the storage destroyed, before equipment is reused or disposed of?",
        M365,
        "Before devices and storage are reused or disposed of, data is securely wiped [with an Intune wipe or a "
        "certified erasure tool] or the storage is destroyed by [certified disposal company], and the certificate is "
        "kept.",
    ),
    gap(
        "removable_media",
        "Is the use of USB drives and other removable media controlled?",
        f"{M365} Office Sentry doesn't read Intune device restriction profiles.",
        "Removable media is [blocked by Intune policy or allowed only on encrypted, company-issued drives], and drives "
        "that hold the organisation's data are recorded in the asset register.",
    ),
    gap(
        "web_filtering",
        "Are known malicious websites blocked?",
        M365,
        "Malicious websites are blocked by [Microsoft Defender SmartScreen and network protection, a filtering DNS "
        "service such as Quad9, or the firewall's web filter].",
    ),
    gap(
        "change_management",
        "Are changes to systems planned, tested and approved?",
        M365,
        "Changes to systems, applications and networks are logged in [ticket system], checked for their security "
        "effect, tested where possible and approved by [role] before they're made. Emergency changes are reviewed "
        "afterwards.",
    ),
    gap(
        "log_review",
        "Are security logs and alerts reviewed regularly, and kept safe?",
        f"{M365} It shows whether auditing is on (see the logging answer), not who reads the logs.",
        "Security alerts and sign-in logs are reviewed [daily or weekly] by [team or provider]. Logs are kept for "
        "[period] where users can't change them.",
    ),
)

TOPICS = DATA + GAPS
