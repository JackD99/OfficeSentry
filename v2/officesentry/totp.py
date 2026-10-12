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

from .db import transaction

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


# Recovery codes are stored as an HMAC under a random key, so a copy of the database can't be used to
# try every possible code offline. The key is made on first use and kept in ``settings`` encrypted with the
# vault (whose keys are in secrets.json, never in the database or its backups); rotate-keys re-encrypts it
# like any other secret (``__main__.ENCRYPTED_SETTINGS``), so codes keep working across a rotation.
KEY_SETTING = "totp.recovery_key_enc"
ENCRYPTED_KEYS = (KEY_SETTING,)
_KEYED = "hmac:"  # how a keyed hash starts
# Codes made before keyed hashes (2.2.0) were stored as a bare SHA-256; ``rekey_old_codes`` keeps each as an HMAC
# of that hash under the same key, so they still work and no unkeyed hash stays in the database.
_REKEYED = "hmac-sha256:"


def recovery_key(conn, vault) -> bytes:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (KEY_SETTING,)).fetchone()
    if row is None:
        # Two processes may make one at once: the first to write wins, and both read it back.
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (KEY_SETTING, vault.encrypt(secrets.token_urlsafe(32))),
        )
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (KEY_SETTING,)).fetchone()
    return vault.decrypt(row[0]).encode()


def hash_recovery_code(value: str, key: bytes) -> str:
    return _KEYED + hmac.new(key, normalise_recovery_code(value).encode(), hashlib.sha256).hexdigest()


def _rekeyed(bare: str, key: bytes) -> str:
    return _REKEYED + hmac.new(key, bare.encode(), hashlib.sha256).hexdigest()


def recovery_code_hashes(value: str, key: bytes) -> tuple[str, str]:
    """Every form a stored code can take: keyed, or a code made before keyed hashes, re-keyed."""
    bare = hashlib.sha256(normalise_recovery_code(value).encode()).hexdigest()
    return hash_recovery_code(value, key), _rekeyed(bare, key)


def rekey_old_codes(conn, vault) -> int:
    """Put every recovery code still stored as a bare SHA-256 under the key, and say how many. A bare hash of
    a ten-character code can be found by trying them all, so the portal does this when it starts; sign-in
    only accepts keyed forms."""
    with transaction(conn):
        old = conn.execute("SELECT id, code_hash FROM user_recovery_codes WHERE code_hash NOT LIKE 'hmac%'").fetchall()
        if old:
            key = recovery_key(conn, vault)
            conn.executemany(
                "UPDATE user_recovery_codes SET code_hash = ? WHERE id = ? AND code_hash = ?",
                [(_rekeyed(row["code_hash"], key), row["id"], row["code_hash"]) for row in old],
            )
    return len(old)


def looks_like_recovery_code(value: str) -> bool:
    return len(normalise_recovery_code(value)) == 10
