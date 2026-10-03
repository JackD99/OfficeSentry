"""Self-signed certificate generation for Entra app-only authentication."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ISO = "%Y-%m-%dT%H:%M:%SZ"


@dataclass
class GeneratedCert:
    cert_pem: str
    key_pem: str
    thumbprint: str  # SHA-1, upper-case hex, as Entra and MSAL expect
    not_before: datetime
    not_after: datetime


def generate_certificate(common_name: str = "Office Sentry", years: int = 2) -> GeneratedCert:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC).replace(microsecond=0)
    not_after = now + timedelta(days=365 * years)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    return GeneratedCert(
        cert_pem=cert.public_bytes(serialization.Encoding.PEM).decode(),
        key_pem=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        thumbprint=cert.fingerprint(hashes.SHA1()).hex().upper(),  # noqa: S303 - required by Entra
        not_before=cert.not_valid_before_utc,
        not_after=cert.not_valid_after_utc,
    )


def cert_der(cert_pem: str) -> bytes:
    """DER bytes of the public certificate (the .cer file uploaded to Entra)."""
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    return cert.public_bytes(serialization.Encoding.DER)


def key_credential(cert_pem: str, display_name: str = "Office Sentry") -> dict:
    """Graph ``keyCredential`` payload for uploading the certificate to an app."""
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    return {
        "type": "AsymmetricX509Cert",
        "usage": "Verify",
        "key": base64.b64encode(cert_der(cert_pem)).decode("ascii"),
        "displayName": display_name,
        "startDateTime": cert.not_valid_before_utc.strftime(ISO),
        "endDateTime": cert.not_valid_after_utc.strftime(ISO),
    }
