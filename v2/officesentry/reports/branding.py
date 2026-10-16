"""MSP branding: company name, logo, accent colour, contact details and report footer.

Stored in the ``settings`` table. Templates get a ``Brand`` as ``brand`` (see
``web/branding.py``): client-facing reports, the portal shell and the sign-in
pages. It supplies the CSS variables that colour report covers, headings and
charts and the branded parts of the portal, worked out here so the accent
stays readable whatever colour the MSP picks.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from hashlib import sha256

DEFAULT_NAME = "Office Sentry"
DEFAULT_ACCENT = "#1d5c4b"
MAX_LOGO_BYTES = 512 * 1024
KEYS = (
    "brand_name",
    "brand_logo",
    "brand_accent",
    "brand_contact",
    "brand_support_name",
    "brand_support_email",
    "brand_support_phone",
    "brand_report_footer",
)
MAX_FOOTER = 400
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
LOGO_TYPES = {b"\x89PNG\r\n\x1a\n": "image/png", b"\xff\xd8\xff": "image/jpeg"}

CURRENCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "AUD": "A$", "CAD": "C$", "NZD": "NZ$"}


def money(value: float, currency: str) -> str:
    return f"{CURRENCY_SYMBOLS.get(currency, currency + ' ')}{value:,.2f}"


# -- Colour maths (WCAG relative luminance) ---------------------------------------


def _rgb(hex_: str) -> tuple[int, int, int]:
    return int(hex_[1:3], 16), int(hex_[3:5], 16), int(hex_[5:7], 16)


def _hex(rgb) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def _luminance(hex_: str) -> float:
    def channel(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in _rgb(hex_))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def mix(a: str, b: str, amount: float) -> str:
    """``amount`` of ``b`` mixed into ``a``."""
    return _hex(x + (y - x) * amount for x, y in zip(_rgb(a), _rgb(b), strict=True))


def readable_on(background: str, colour: str, ratio: float = 4.5) -> str:
    """``colour`` darkened or lightened just enough to read as text on ``background``."""
    target = "#000000" if _luminance(background) > 0.18 else "#ffffff"
    for step in range(21):
        candidate = mix(colour, target, step / 20)
        if contrast(candidate, background) >= ratio:
            return candidate
    return target


# -- Brand ------------------------------------------------------------------------


@dataclass(frozen=True)
class Brand:
    name: str = ""
    logo: str = ""  # data: URI (PNG or JPEG), or ""
    accent: str = DEFAULT_ACCENT
    contact: str = ""
    # Who clients should contact, shown on their portal home page.
    support_name: str = ""
    support_email: str = ""
    support_phone: str = ""
    report_footer: str = ""  # a note or disclaimer printed at the end of every client report

    @property
    def has_support(self) -> bool:
        return bool(self.support_name or self.support_email or self.support_phone)

    @property
    def display_name(self) -> str:
        return self.name or DEFAULT_NAME

    @property
    def is_default(self) -> bool:
        return not (self.name or self.logo or self.contact) and self.accent == DEFAULT_ACCENT

    @property
    def on_accent(self) -> str:
        """Text colour on a solid accent background (the cover band)."""
        return "#ffffff" if contrast("#ffffff", self.accent) >= contrast("#111827", self.accent) else "#111827"

    @property
    def version(self) -> str:
        """Changes whenever the brand does, for cache-busting the stylesheet URL."""
        return sha256("|".join((self.name, self.logo, self.accent, self.contact)).encode()).hexdigest()[:12]

    @property
    def css(self) -> str:
        """CSS variables for report.css. Only validated hex colours go in, so it's safe inline."""
        # The ink is readable on its soft background (tags like "New" sit on it), so on plain paper too.
        light_soft, dark_soft = mix(self.accent, "#ffffff", 0.92), mix(self.accent, "#181e27", 0.82)
        light_ink = readable_on(light_soft, self.accent)
        dark_ink = readable_on(dark_soft, self.accent)
        dark = f"--brand-ink:{dark_ink};--brand-soft:{dark_soft};--brand-line:{mix(self.accent, '#181e27', 0.55)};"
        return (
            ":root{"
            f"--brand:{self.accent};--brand-ink:{light_ink};--on-brand:{self.on_accent};"
            f"--brand-soft:{light_soft};--brand-line:{mix(self.accent, '#ffffff', 0.7)};"
            f"--brand-cover-2:{mix(self.accent, '#000000', 0.25)};--brand-ink-dark:{dark_ink};"
            "}"
            # A page drawn light on purpose (data-theme="light": PDFs, print pages, report previews, or a
            # viewer who chose Light) keeps the light colours; one who chose Dark gets the dark ones anywhere.
            '@media (prefers-color-scheme: dark){:root:not([data-theme="light"]) body.report:not(.pdf){' + dark + "}}"
            ':root[data-theme="dark"] body.report:not(.pdf){' + dark + "}"
        )


def logo_image(brand: Brand) -> tuple[str, bytes] | None:
    """The logo as (mimetype, bytes), for attaching to an email."""
    if not brand.logo.startswith("data:image/") or ";base64," not in brand.logo:
        return None
    head, data = brand.logo.split(";base64,", 1)
    try:
        return head[5:], base64.b64decode(data)
    except ValueError:
        return None


def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) of a PNG or JPEG, or None. Email clients need both on the <img>."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data.startswith(b"\xff\xd8"):
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                return None
            marker, length = data[i + 1], int.from_bytes(data[i + 2 : i + 4], "big")
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                return int.from_bytes(data[i + 7 : i + 9], "big"), int.from_bytes(data[i + 5 : i + 7], "big")
            i += 2 + length
    return None


def fit(size: tuple[int, int] | None, height: int, max_width: int) -> tuple[int, int] | None:
    """The size to show an image at: ``height`` tall, narrower if it would be wider than ``max_width``."""
    if not size or not all(size):
        return None
    w, h = size
    width = round(w * height / h)
    if width > max_width:
        width, height = max_width, max(1, round(h * max_width / w))
    return width, height


def load(conn) -> Brand:
    rows = {
        r["key"]: r["value"]
        for r in conn.execute(f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(KEYS))})", KEYS)
    }
    accent = rows.get("brand_accent") or DEFAULT_ACCENT
    return Brand(
        name=rows.get("brand_name") or "",
        logo=rows.get("brand_logo") or "",
        accent=accent if HEX_RE.match(accent) else DEFAULT_ACCENT,
        contact=rows.get("brand_contact") or "",
        support_name=rows.get("brand_support_name") or "",
        support_email=rows.get("brand_support_email") or "",
        support_phone=rows.get("brand_support_phone") or "",
        report_footer=rows.get("brand_report_footer") or "",
    )


def logo_data_uri(data: bytes) -> str | None:
    """A data: URI for a PNG or JPEG logo, or None if it's neither or too large.

    SVG is refused on purpose: it can carry script, and the logo is shown to
    every client who opens a report.
    """
    if not data or len(data) > MAX_LOGO_BYTES:
        return None
    for magic, mime in LOGO_TYPES.items():
        if data.startswith(magic):
            return f"data:{mime};base64,{base64.b64encode(data).decode()}"
    return None
