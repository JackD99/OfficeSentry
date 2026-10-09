"""How strong each person's sign-in methods are, and what the methods policy says.

Shared by the Passwords and sign-in methods report, its checks and the account
page. Reads stored MFA registration rows (``methodsRegistered`` from
Microsoft's registration report, or method types read one user at a time) and
the stored authentication methods policy; never Graph.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Strength tiers, strongest first.
PHISHING_RESISTANT = "Phishing-resistant"
APP = "Authenticator app or token"
PHONE = "Phone (SMS or voice call)"
NO_SECOND_FACTOR = "None"
TIERS = (PHISHING_RESISTANT, APP, PHONE)
# Methods that exist but can't be a second factor at sign-in.
RESET_ONLY = "Password reset or setup only"

TIER_MEANING = {
    PHISHING_RESISTANT: "Bound to the real Microsoft sign-in page, so a fake page can't relay it.",
    APP: "Strong, but a convincing fake sign-in page can relay a code or push approval.",
    PHONE: "Weakest second factor: texts and calls can be intercepted or moved to another SIM.",
    NO_SECOND_FACTOR: "No second factor: the password alone gets in.",
}

# methodsRegistered values (registration report) and method types (read per user), lower-cased.
METHODS: dict[str, tuple[str, str]] = {
    "fido2securitykey": ("Security key (FIDO2)", PHISHING_RESISTANT),
    "fido2": ("Security key or passkey (FIDO2)", PHISHING_RESISTANT),
    "passkeydevicebound": ("Passkey", PHISHING_RESISTANT),
    "passkeydeviceboundauthenticator": ("Passkey in Authenticator", PHISHING_RESISTANT),
    "passkeydeviceboundwindowshello": ("Passkey in Windows Hello", PHISHING_RESISTANT),
    "passkey": ("Passkey", PHISHING_RESISTANT),
    "passkeysynced": ("Synced passkey", PHISHING_RESISTANT),  # in a password manager; seen on the test tenant
    "windowshelloforbusiness": ("Windows Hello for Business", PHISHING_RESISTANT),
    "x509certificatemultifactor": ("Certificate", PHISHING_RESISTANT),
    "x509certificate": ("Certificate", PHISHING_RESISTANT),
    "macossecureenclavekey": ("Mac Platform SSO", PHISHING_RESISTANT),
    "platformcredential": ("Mac Platform SSO", PHISHING_RESISTANT),
    "microsoftauthenticatorpush": ("Authenticator app", APP),
    "microsoftauthenticator": ("Authenticator app", APP),
    "microsoftauthenticatorpasswordless": ("Authenticator phone sign-in", APP),
    "softwareonetimepasscode": ("Authenticator code (OATH)", APP),
    "softwareoath": ("Authenticator code (OATH)", APP),
    "hardwareonetimepasscode": ("Hardware token (OATH)", APP),
    "hardwareoath": ("Hardware token (OATH)", APP),
    "externalauthenticationmethod": ("External MFA provider", APP),
    "mobilephone": ("Mobile phone (SMS or call)", PHONE),
    "alternatemobilephone": ("Alternate mobile phone", PHONE),
    "officephone": ("Office phone (call)", PHONE),
    "phone": ("Phone (SMS or call)", PHONE),
    "email": ("Email", RESET_ONLY),
    "securityquestion": ("Security questions", RESET_ONLY),
    "temporaryaccesspass": ("Temporary Access Pass", RESET_ONLY),
}
NOT_METHODS = {"password", "apppassword", "none", ""}

# Default and system-preferred methods (registration report), lower-cased.
PREFERRED = {
    "push": "Authenticator app", "oath": "Authenticator code", "sms": "Text message (SMS)",
    "voicemobile": "Voice call", "voicealternatemobile": "Voice call", "voiceoffice": "Voice call (office)",
    "fido2": "Passkey or security key", "passkey": "Passkey", "x509certificate": "Certificate",
    "windowshelloforbusiness": "Windows Hello", "email": "Email", "temporaryaccesspass": "Temporary Access Pass",
}


@dataclass
class Strength:
    """One person's registered methods, by tier."""
    methods: list[tuple[str, str]] = field(default_factory=list)  # (label, tier), strongest first

    @property
    def tiers(self) -> set[str]:
        return {t for _, t in self.methods}

    @property
    def strongest(self) -> str:
        return next((t for t in TIERS if t in self.tiers), NO_SECOND_FACTOR)

    @property
    def phishing_resistant(self) -> bool:
        return PHISHING_RESISTANT in self.tiers

    @property
    def phone_only(self) -> bool:
        """SMS or a voice call is the only second factor."""
        return self.strongest == PHONE

    @property
    def labels(self) -> list[str]:
        return [label for label, _ in self.methods]


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def strength(reg: dict | None) -> Strength | None:
    """None when there's no registration row for the person."""
    if reg is None:
        return None
    raw = reg.get("methodsRegistered")
    order = {t: i for i, t in enumerate((*TIERS, RESET_ONLY))}
    found: dict[str, str] = {}
    for m in raw if isinstance(raw, list) else []:
        key = _text(m).lower()
        if key in NOT_METHODS:
            continue
        label, tier = METHODS.get(key, (_text(m), RESET_ONLY))
        found.setdefault(label, tier)
    return Strength(sorted(found.items(), key=lambda m: order.get(m[1], len(order))))


def preferred(reg: dict | None) -> str:
    """What the person is asked for first: the system-preferred method when that's on, else their default."""
    reg = reg or {}
    system = reg.get("systemPreferredAuthenticationMethods")
    if reg.get("isSystemPreferredAuthenticationMethodEnabled") and isinstance(system, list):
        names = [PREFERRED.get(_text(m).lower(), _text(m)) for m in system if _text(m).lower() not in NOT_METHODS]
        if names:
            return names[0]
    for key in ("userPreferredMethodForSecondaryAuthentication", "defaultMfaMethod"):
        value = _text(reg.get(key))
        if value.lower() not in NOT_METHODS:
            return PREFERRED.get(value.lower(), value)
    return ""


def asks_for_phone(reg: dict | None) -> bool:
    """Prompted for a text or call first although something stronger is registered."""
    first = preferred(reg)
    return first.startswith(("Text message", "Voice call"))


# -- The authentication methods policy -------------------------------------------------------

ALL_USERS = "all_users"


@dataclass(frozen=True)
class PolicyMethod:
    label: str
    recommended: str
    want_on: bool | None  # True: should be on; False: should be off; None: the MSP's choice
    severity: str | None  # when it isn't as recommended
    why: str = ""


POLICY_METHODS: dict[str, PolicyMethod] = {
    "Fido2": PolicyMethod("Passkeys and security keys (FIDO2)", "On", True, "medium",
                          "Needed for phishing-resistant sign-in, which admins should use."),
    "MicrosoftAuthenticator": PolicyMethod("Microsoft Authenticator", "On", True, "medium",
                                           "The second factor most people should use."),
    "Sms": PolicyMethod("Text message (SMS)", "Off, once people use Authenticator", False, "low",
                        "Texts can be intercepted or moved to another SIM."),
    "Voice": PolicyMethod("Voice call", "Off", False, "low", "Calls can be redirected, and are easy to approve "
                                                            "by mistake."),
    "Email": PolicyMethod("Email one-time passcode", "Off, unless guests or password reset need it", None, None,
                          "Not a second factor for members; only for password reset and guests."),
    "SoftwareOath": PolicyMethod("Other authenticator apps (OATH codes)", "Your choice", None, None),
    "HardwareOath": PolicyMethod("Hardware tokens (OATH)", "Your choice", None, None),
    "TemporaryAccessPass": PolicyMethod("Temporary Access Pass", "On, for setting up passkeys", None, None,
                                        "A time-limited pass for setting up a new phone or passkey."),
    "X509Certificate": PolicyMethod("Certificate-based authentication", "Your choice", None, None),
    "QRCodePin": PolicyMethod("QR code sign-in", "Your choice", None, None),
}
# Methods listed first in the policy table: the ones that decide how strong sign-in is.
POLICY_ORDER = ("Fido2", "MicrosoftAuthenticator", "Sms", "Voice", "Email", "TemporaryAccessPass", "SoftwareOath",
                "HardwareOath", "X509Certificate", "QRCodePin")

MANAGED = {"default": "Microsoft managed", "enabled": "On", "disabled": "Off"}


def targets(value, groups: dict[str, str]) -> list[str]:
    """Group targets as names: "All users", a group's name, or "Group 1a2b3c4d" when it couldn't be read."""
    out = []
    for t in value if isinstance(value, list) else []:
        if not isinstance(t, dict):
            continue
        gid = _text(t.get("id"))
        if gid == ALL_USERS:
            out.append("All users")
        elif gid:
            out.append(groups.get(gid) or f"Group {gid[:8]}")
    return out


def who(item: dict, groups: dict[str, str]) -> str:
    """'All users', 'Admins, Helpdesk', 'All users except Contractors', or 'Nobody'."""
    include = targets(item.get("includeTargets"), groups)
    exclude = targets(item.get("excludeTargets"), groups)
    text = ", ".join(include) or "Nobody"
    return f"{text} except {', '.join(exclude)}" if exclude and include else text


def is_on(item: dict | None) -> bool:
    return bool(item) and item.get("state") == "enabled" and bool(targets(item.get("includeTargets"), {}))


# Built-in authentication strengths, and the combinations that are phishing-resistant.
PHISHING_RESISTANT_COMBINATIONS = {"windowsHelloForBusiness", "fido2", "x509CertificateMultiFactor"}
PHISHING_RESISTANT_STRENGTH = "00000000-0000-0000-0000-000000000004"


def strength_is_phishing_resistant(s: dict) -> bool:
    combos = s.get("allowedCombinations")
    if isinstance(combos, list) and combos:
        return all(isinstance(c, str) and c in PHISHING_RESISTANT_COMBINATIONS for c in combos)
    return s.get("id") == PHISHING_RESISTANT_STRENGTH
