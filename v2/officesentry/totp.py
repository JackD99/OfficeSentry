"""Time-based one-time passwords (RFC 6238) for portal sign-in, and recovery codes.

Works with any authenticator app (Microsoft Authenticator, Google Authenticator,
1Password, ...): SHA-1, 6 digits, 30-second steps.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import secrets
import struct
import time
from urllib.parse import quote, urlencode

import segno

DIGITS = 6
STEP = 30
WINDOW = 1  # accept the previous and next step too, for clock drift
ISSUER = "Office Sentry"
RECOVERY_CODES = 10
# No 0/o, 1/l/i: recovery codes get read off paper.
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code_at(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP)


def code(secret: str, now: float | None = None) -> str:
    return _code_at(secret, current_step(now))


def verify(secret: str, candidate: str, last_step: int | None = None, now: float | None = None) -> int | None:
    """Return the matched time step, or None.

    A step at or before ``last_step`` is refused so a code can't be replayed.
    """
    candidate = "".join(ch for ch in candidate or "" if not ch.isspace())
    if len(candidate) != DIGITS or not candidate.isdigit():
        return None
    step = current_step(now)
    for s in range(step - WINDOW, step + WINDOW + 1):
        if last_step is not None and s <= last_step:
            continue
        if hmac.compare_digest(_code_at(secret, s), candidate):
            return s
    return None


def provisioning_uri(secret: str, account: str) -> str:
    label = quote(f"{ISSUER}:{account}")
    query = urlencode({"secret": secret, "issuer": ISSUER, "digits": DIGITS, "period": STEP})
    return f"otpauth://totp/{label}?{query}"


def qr_svg(data: str) -> str:
    """Inline SVG QR code (no external assets, so the CSP stays strict)."""
    buf = io.BytesIO()
    segno.make(data, error="m").save(
        buf,
        kind="svg",
        xmldecl=False,
        svgns=True,
        scale=5,
        border=3,
        dark="#111827",
        light="#ffffff",
        svgclass=None,
        lineclass=None,
    )
    return buf.getvalue().decode()


def grouped(secret: str) -> str:
    """The secret in groups of four, for typing into an app by hand."""
    return " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))


# -- Recovery codes ------------------------------------------------------------


def new_recovery_codes(n: int = RECOVERY_CODES) -> list[str]:
    def one():
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
        return f"{raw[:5]}-{raw[5:]}"

    return [one() for _ in range(n)]


def normalise_recovery_code(value: str) -> str:
    return "".join(ch for ch in (value or "").lower() if ch.isalnum())


def hash_recovery_code(value: str) -> str:
    return hashlib.sha256(normalise_recovery_code(value).encode()).hexdigest()


def looks_like_recovery_code(value: str) -> bool:
    return len(normalise_recovery_code(value)) == 10
