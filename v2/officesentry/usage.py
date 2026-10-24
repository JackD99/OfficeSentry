"""Microsoft 365 usage per person, and licence right-sizing.

Reads the snapshot written by ``collectors/usage.py``. ``people(snap)`` gives
everyone's usage keyed by lower-case sign-in name, and ``person(snap, upn)``
one person's, for any page that shows a single account. ``rightsize`` decides
whether someone could move to a cheaper licence; it reads usage only, never
Graph.

Microsoft's usage reports run a couple of days behind and cover the last
``ACTIVE_DAYS`` days. When the tenant conceals names in usage reports, every
sign-in name is a 32-character code; the codes are stable across reports, so
people still line up with each other, just not with the tenant's accounts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .licences import FREE_SKUS, SKU_NAMES
from .snapshot import TenantSnapshot
from .text import plural

ACTIVE_DAYS = 30
COLLECTOR = "m365_usage"

# (key, label) in the order pages show them. "office" is the Office apps on any platform.
SERVICES = (
    ("exchange", "Email"),
    ("teams", "Teams"),
    ("onedrive", "OneDrive"),
    ("sharepoint", "SharePoint"),
    ("office", "Office apps"),
)
APPS = (
    ("outlook", "Outlook"),
    ("word", "Word"),
    ("excel", "Excel"),
    ("powerPoint", "PowerPoint"),
    ("oneNote", "OneNote"),
    ("teams", "Teams"),
)
PLATFORMS = (("windows", "Windows"), ("mac", "Mac"), ("mobile", "Mobile"), ("web", "Web"))
# (key, label) of the mail apps in Microsoft's email apps report. Each person's ``mailApps`` holds, per key, the
# versions Microsoft saw ("Undetermined" when it can't tell), and nothing for an app they didn't use.
MAIL_APPS = (
    ("windows", "Outlook for Windows"),
    ("mac", "Outlook for Mac"),
    ("mobile", "Outlook mobile"),
    ("web", "Outlook on the web"),
    ("macMail", "Mail for Mac"),
    ("otherMobile", "Other mobile mail apps"),
    ("pop", "POP"),
    ("imap", "IMAP"),
    ("smtp", "SMTP"),
)
PROTOCOLS = ("pop", "imap", "smtp")
# When each Outlook release for Windows and Mac leaves Microsoft's support (its lifecycle dates), by the year in
# its name. Microsoft 365's Outlook ("Outlook M365") is kept up to date, so it has none.
OUTLOOK_SUPPORT_ENDS = {
    2007: date(2017, 10, 10),
    2010: date(2020, 10, 13),
    2013: date(2023, 4, 11),
    2016: date(2025, 10, 14),
    2019: date(2025, 10, 14),
    2021: date(2026, 10, 13),
    2024: date(2029, 10, 9),
}
_YEAR = re.compile(r"\b(20\d\d)\b")

HASHED = re.compile(r"^[0-9A-Fa-f]{32}$")

CONCEALED_NOTICE = (
    "Microsoft is hiding people's names in this tenant's usage reports, so people show as codes and can't be "
    "matched to their accounts or licences by name. To show names: sign in to admin.microsoft.com as a Global "
    'Admin, open Settings, then Org settings, then Reports on the Services tab. Untick "Display concealed user, '
    'group, and site names in all reports" and save. Names appear after the next collection.'
)

# -- Licences -------------------------------------------------------------------------------

COPILOT = "Microsoft_365_Copilot"
EXCHANGE_P1, EXCHANGE_P2 = "EXCHANGESTANDARD", "EXCHANGEENTERPRISE"
# Plans that include the desktop Office apps, and what the same plan costs without them.
# None: there's no like-for-like plan without desktop apps (the security features would go too).
# "": an apps-only plan, so the cheaper option is no plan at all.
WITHOUT_DESKTOP = {
    "O365_BUSINESS_PREMIUM": "O365_BUSINESS_ESSENTIALS",
    "ENTERPRISEPACK": "STANDARDPACK",
    "SMB_BUSINESS": "",
    "OFFICESUBSCRIPTION": "",
    "SPB": None,
    "SPE_E3": None,
    "SPE_E5": None,
    "ENTERPRISEPREMIUM": None,
}
# Suites that include a mailbox, so someone who only uses email could have an Exchange Online plan instead.
MAIL_SUITES = {
    "O365_BUSINESS_ESSENTIALS",
    "O365_BUSINESS_PREMIUM",
    "SPB",
    "STANDARDPACK",
    "ENTERPRISEPACK",
    "ENTERPRISEPREMIUM",
    "SPE_E3",
    "SPE_E5",
    "SPE_F1",
    "DESKLESSPACK",
}
# Suites whose security features (Intune, Entra ID P1, Defender) a mail-only plan doesn't have.
SECURITY_SUITES = {"SPB", "SPE_E3", "SPE_E5", "ENTERPRISEPREMIUM"}
# Plans whose mailbox is 50 GB with no litigation hold: no home for a bigger mailbox, an archive or a hold.
SMALL_MAILBOX = {"O365_BUSINESS_ESSENTIALS", "STANDARDPACK", EXCHANGE_P1}
# Plans right-sizing can suggest; the prices page lists them so savings can be priced.
TARGETS = ("O365_BUSINESS_ESSENTIALS", "STANDARDPACK", EXCHANGE_P1, EXCHANGE_P2)
# Exchange Online Plan 1 holds 50 GB with no archive; leave headroom before suggesting it.
PLAN1_MAX_BYTES = 45 * 1024**3

_BY_PRODUCT = {name.upper(): part for part, name in SKU_NAMES.items()}


def part_for_product(name: str | None) -> str | None:
    """SKU part number for a usage report's product name ("MICROSOFT 365 BUSINESS STANDARD")."""
    return _BY_PRODUCT.get((name or "").strip().upper())


# -- Reading the snapshot ---------------------------------------------------------------------


def concealed(upn: str | None) -> bool:
    return bool(HASHED.match(upn or ""))


def label(row: dict) -> str:
    """The person's name, or a short stable code when Microsoft hides names."""
    if row.get("concealed"):
        return f"Hidden name ({(row.get('upn') or '?')[:6]})"
    return row.get("upn") or row.get("name") or "Unknown"


def owner_label(row: dict) -> str | None:
    """A SharePoint site's or OneDrive's owner from the usage reports, a hidden one shown as ``label`` shows people
    (the OneDrive report's code is the usage reports' code in lower case)."""
    owner = row.get("ownerPrincipalName") or row.get("owner")
    if (isinstance(owner, str) and concealed(owner)) or (row.get("concealed") and not owner):
        return label({"concealed": True, "upn": owner.upper() if isinstance(owner, str) else None})
    return owner


def status(snap: TenantSnapshot) -> dict | None:
    """The collector's status object, or None when usage hasn't been collected."""
    rows = snap.objects(COLLECTOR, "usage_status")
    return rows[0] if rows else None


def people(snap: TenantSnapshot) -> dict[str, dict]:
    """Everyone in the usage reports, keyed by lower-case sign-in name (or code)."""
    return {(r.get("upn") or "").lower(): r for r in snap.objects(COLLECTOR, "user_usage") or []}


def person(snap: TenantSnapshot, upn: str | None) -> dict | None:
    """One person's usage for a page about a single account, or None when they aren't in the reports.

    The row has ``active`` and ``lastActive`` per service key in ``SERVICES``,
    ``licensed`` per service, ``platforms`` (Office apps by platform, None when
    unknown), ``apps``, ``activations``, ``email`` and ``teamsCounts``.
    """
    return people(snap).get((upn or "").lower()) if upn else None


def blocked(snap: TenantSnapshot) -> set[str]:
    """Lower-case sign-in names of accounts blocked from signing in. They can't use anything, so adoption
    figures leave them out; Licence waste counts their licences."""
    return {
        u["userPrincipalName"].lower()
        for u in snap.objects("users", "user") or []
        if u.get("accountEnabled") is False and isinstance(u.get("userPrincipalName"), str)
    }


def desktop_licensed(row: dict) -> bool:
    """Holds a plan that includes the desktop Office apps (Business Standard, E3), not a web-only one (Business
    Basic, E1, F3)."""
    return any(part_for_product(p) in WITHOUT_DESKTOP for p in row.get("products") or [])


def licensed(row: dict) -> bool:
    """Holds a licence for a service, or any product that isn't a free one (Power Automate Free and the like)."""
    return any((row.get("licensed") or {}).values()) or any(
        "FREE" not in (p or "").upper() for p in row.get("products") or []
    )


def any_activity(row: dict) -> bool:
    return any((row.get("active") or {}).values()) or bool(row.get("copilotActive"))


def desktop_use(row: dict) -> bool | None:
    """Used the Office apps on Windows or a Mac in the period; None when the apps report wasn't read."""
    platforms = row.get("platforms")
    if platforms is None:
        return None
    return bool(platforms.get("windows") or platforms.get("mac"))


def email_only(row: dict) -> bool:
    """Active in email and nothing else: no Teams, files, desktop apps or web apps other than Outlook.

    False when the apps report wasn't read, since desktop app use can't be ruled out.
    """
    active = row.get("active") or {}
    if not active.get("exchange") or any(active.get(k) for k in ("teams", "onedrive", "sharepoint")):
        return False
    if desktop_use(row) is not False:
        return False
    apps = row.get("apps") or {}
    return not any(apps.get(k) for k, _ in APPS if k != "outlook")


def outlook_support_ends(version: str) -> date | None:
    """When an Outlook version from the email apps report ("Outlook 2016") leaves support; None for Microsoft 365's
    and for a version Microsoft couldn't tell."""
    found = _YEAR.search(version or "")
    return OUTLOOK_SUPPORT_ENDS.get(int(found.group(1))) if found else None


def old_outlook(row: dict, today: date) -> list[str]:
    """The Outlook versions someone used on Windows or a Mac that are out of support by ``today``."""
    apps = row.get("mailApps") or {}
    return sorted(
        {
            v
            for key in ("windows", "mac")
            for v in apps.get(key) or []
            if (end := outlook_support_ends(v)) and end <= today
        }
    )


def used_services(row: dict) -> list[str]:
    """Services used in the period. Outlook alone counts as email, not as using the Office apps."""
    active = dict(row.get("active") or {})
    if active.get("office") and not desktop_use(row) and set(row.get("apps") or {}) <= {"outlook"}:
        active["office"] = row.get("platforms") is None  # no app detail: trust Microsoft's last activity date
        active["exchange"] = True
    return [name for key, name in SERVICES if active.get(key)]


# -- Right-sizing -----------------------------------------------------------------------------


@dataclass
class Suggestion:
    current: str  # SKU part number the person holds
    target: str | None  # cheaper part number; "" to remove the licence; None when there's no single plan
    why: str
    caution: str = ""
    keep_mailbox: bool = False  # removing it would delete a mailbox that still receives mail

    @property
    def action(self) -> str:
        if self.target == "" and self.keep_mailbox:
            return "Convert to a shared mailbox"
        if self.target == "":
            return "Remove the licence"
        if self.target is None:
            return "A plan without desktop apps"
        return SKU_NAMES.get(self.target, self.target)

    def saving(self, prices: dict[str, float]) -> float | None:
        """Monthly saving from the MSP's prices; None when either side isn't priced."""
        now = prices.get(self.current)
        if now is None or self.target is None:
            return None
        then = 0.0 if self.target == "" else prices.get(self.target)
        return None if then is None else round(max(now - then, 0), 2)


@dataclass
class Holder:
    """The paid licences one person holds, and where that came from."""

    parts: list[str] = field(default_factory=list)
    from_products: bool = False  # read from the usage report's product names, not the account


def paid_parts(row: dict, user: dict | None, sku_parts: dict[str, str]) -> Holder:
    """Paid SKU part numbers for a person: from their account when it matches, else the report's product names."""
    if user is not None:
        parts = [sku_parts.get(s) for s in user.get("skuIds") or []]
        return Holder([p for p in dict.fromkeys(parts) if p and p not in FREE_SKUS])
    parts = [part_for_product(p) for p in row.get("products") or []]
    return Holder([p for p in dict.fromkeys(parts) if p and p not in FREE_SKUS], from_products=True)


def _gb(n: float) -> str:
    return f"{n / 1024**3:.1f} GB"


def rightsize(
    row: dict, held: list[str], mailbox: dict | None = None, relies_on_security: bool = False
) -> list[Suggestion]:
    """Cheaper licences for one person, from what they used in the last ``ACTIVE_DAYS`` days.

    Rules, most savings first: nothing used at all; Copilot unused; only email;
    no desktop Office apps; Exchange Online Plan 2 on a small mailbox with no
    archive or hold. A licence gets at most one suggestion. Someone who
    ``relies_on_security`` (a device managed by Intune, or an admin role) isn't
    moved off a suite with Intune and Entra ID P1: their device would fall out of
    compliance and Conditional Access. A mailbox over Plan 1's size, with an
    archive or on hold isn't moved to a plan that can't keep it.
    """
    held = list(dict.fromkeys(held))
    if not held:
        return []
    if not any_activity(row):
        why = f"No activity in email, Teams, OneDrive, SharePoint or the Office apps in the last {ACTIVE_DAYS} days"
        received = (row.get("email") or {}).get("received") or 0
        if received and (row.get("licensed") or {}).get("exchange"):
            # Removing the licence deletes the mailbox; a shared mailbox keeps it for free (up to 50 GB).
            return [
                Suggestion(
                    p,
                    "",
                    f"{why}, but the mailbox still received {plural(received, 'message')}. A shared "
                    "mailbox keeps it without a licence",
                    keep_mailbox=True,
                )
                for p in held
            ]
        return [Suggestion(p, "", why) for p in held]
    out: list[Suggestion] = []
    if COPILOT in held and row.get("copilotActive") is False:
        out.append(Suggestion(COPILOT, "", f"Hasn't used Copilot in the last {ACTIVE_DAYS} days"))
    big = bool(
        mailbox
        and (
            (mailbox.get("totalBytes") or 0) > PLAN1_MAX_BYTES
            or mailbox.get("archive")
            or mailbox.get("litigationHold")
        )
    )
    suites = [p for p in held if p in MAIL_SUITES or p in WITHOUT_DESKTOP]
    for part in suites:
        if part in SECURITY_SUITES and relies_on_security:
            continue
        if part in MAIL_SUITES and email_only(row):
            if big and part == "O365_BUSINESS_ESSENTIALS":
                continue  # Exchange Online Plan 2, the plan that could keep it, costs more than Business Basic
            target = EXCHANGE_P2 if big else EXCHANGE_P1
            caution = (
                "It also includes Intune, Entra ID P1 and Defender, which an Exchange Online plan doesn't. "
                "Check the account doesn't rely on them."
                if part in SECURITY_SUITES
                else ""
            )
            out.append(Suggestion(part, target, f"Only used email in the last {ACTIVE_DAYS} days", caution))
        elif part in WITHOUT_DESKTOP and desktop_use(row) is False:
            target = WITHOUT_DESKTOP[part]
            if big and target in SMALL_MAILBOX:
                continue  # its 50 GB mailbox has no room for this one, or no hold or archive
            caution = (
                "No plan matches it without desktop apps; compare the web-only plans with the security "
                "features they need."
                if target is None
                else ""
            )
            out.append(
                Suggestion(
                    part,
                    target,
                    f"Hasn't used the desktop Office apps (Windows or Mac) in the last {ACTIVE_DAYS} days",
                    caution,
                )
            )
    if EXCHANGE_P2 in held and mailbox and not big:
        out.append(
            Suggestion(
                EXCHANGE_P2,
                EXCHANGE_P1,
                f"Mailbox is {_gb(mailbox.get('totalBytes') or 0)}, under Plan 1's 50 GB, with no archive or hold",
            )
        )
    return out


# -- Copilot ----------------------------------------------------------------------------------

# Microsoft 365 Copilot in each app: (key, label, the field prefix in Microsoft's per-person Copilot report, the
# prefix in its daily counts), in the order pages show them.
COPILOT_APPS = (
    ("chat", "Copilot Chat", "copilotChat", "copilotChat"),
    ("teams", "Teams", "microsoftTeamsCopilot", "microsoftTeams"),
    ("outlook", "Outlook", "outlookCopilot", "outlook"),
    ("word", "Word", "wordCopilot", "word"),
    ("excel", "Excel", "excelCopilot", "excel"),
    ("powerPoint", "PowerPoint", "powerPointCopilot", "powerPoint"),
    ("oneNote", "OneNote", "oneNoteCopilot", "oneNote"),
    ("loop", "Loop", "loopCopilot", "loop"),
)
# Every Microsoft 365 Copilot licence carries this service plan, whatever it's called (the add-on, Copilot Business,
# E7, a suite bundled with Copilot), so it tells a Copilot licence where part numbers keep changing.
COPILOT_PLAN = "M365_COPILOT_APPS"


def _plans(sku: dict) -> list[str]:
    plans = sku.get("servicePlans") if isinstance(sku.get("servicePlans"), list) else []
    return [p.get("servicePlanName") or "" for p in plans if isinstance(p, dict)]


def copilot_skus(skus: list[dict] | None) -> dict[str, dict]:
    """The tenant's licences that include Microsoft 365 Copilot, by SKU ID."""
    return {
        s["skuId"]: s
        for s in skus or []
        if isinstance(s, dict) and isinstance(s.get("skuId"), str) and COPILOT_PLAN in _plans(s)
    }


def copilot_addon(sku: dict) -> bool:
    """Copilot bought on its own, rather than as part of a suite with a mailbox (E7, a bundle): only an add-on's
    price is what an unused Copilot licence costs."""
    return not any(p.startswith("EXCHANGE_S_") for p in _plans(sku))
