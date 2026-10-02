"""Encryption of credentials at rest (Fernet, rotatable via MultiFernet)."""

from __future__ import annotations

from cryptography.fernet import Fernet, MultiFernet


class Vault:
    def __init__(self, keys: list[str]):
        if not keys:
            raise ValueError("at least one encryption key is required")
        self._fernet = MultiFernet([Fernet(k.encode() if isinstance(k, str) else k) for k in keys])

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()

    def rotate(self, token: str) -> str:
        """Re-encrypt a value with the current (first) key."""
        return self._fernet.rotate(token.encode()).decode()
