"""No secret or private key reaches a browser, whoever asks.

One of every kind of secret Office Sentry holds is planted (each app connection's private key and the one
waiting to replace it, the mail app's key, client secret and SMTP password, the Microsoft sign-in app's
keys, the alert webhook's address and signing secret, the monitoring token, a device code in flight,
someone else's two-step secret, recovery codes, password hash and session, the session and encryption keys,
and every encrypted value as stored; private keys as PEM text and as DER). Then every GET route in the URL
map is opened, each download in every format, as an Administrator, an analyst and a client account, and
every byte of every answer is searched: the page, its headers, each file inside a spreadsheet, Word file or
zip, and the page a PDF is made from. So are the log files the crawl wrote. The support package has its own
test of this (``test_support.py``).
"""

from __future__ import annotations

import base64
import io
import zipfile

import pytest
from cryptography import x509
from flask import Response

from officesentry import alerts, cert_lifecycle, logs, sso, store, system_health, totp
from officesentry.certs import generate_certificate
from officesentry.db import utcnow
from tests.conftest import PASSWORD, PASSWORD_HASH, login, make_user
from tests.test_isolation_walk import _a_urls, _pdf_as_page, world  # noqa: F401  (fixtures)

MAIL_SECRET = "MailClientSecretQ8~zLmN3pXv"
SMTP_PASSWORD = "SmtpPassw0rdKx93hdQQ"
WEBHOOK_KEY = "hookKeyAa11Bb22Cc33"
SIGNING_SECRET = "AlertSigningSecret55aa77bb"
DEVICE_CODE = "DAQABAAEAAADeviceCodeSecretZZ0123456789"
OTHER_TOTP = "JBSWY3DPEHPK3PXPVICTIMTOTPAAAAAA"
OTHER_PENDING_TOTP = "KRSXG5DTNFTW4ZDBMVSXI5DVMVZGKZZA"


def _key(label: str, pem: str) -> dict[str, bytes]:
    """40 bytes from the end of an RSA private key, as PEM text (within one line, so found however its lines
    are broken) and as DER. The key's last field is private (the start holds the modulus, which the
    certificate carries too), so they're found only with the key."""
    lines = pem.strip().splitlines()[1:-1]
    return {f"{label} (PEM)": lines[-2][10:50].encode(), f"{label} (DER)": base64.b64decode("".join(lines))[-40:]}


def _setting(db, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


@pytest.fixture
def planted(app, db, vault, cfg, world):  # noqa: F811
    """The isolation walk's two clients and their objects, one of every secret, and what to search for."""
    alpha, bravo, ids = world
    secrets: dict[str, str | bytes] = {}

    connection = store.get_connection(db, ids["connection_id"])
    secrets |= _key("app connection key", vault.decrypt(connection["key_enc"]))
    secrets |= _key("app connection's next key", cert_lifecycle.start(db, vault, ids["connection_id"], "x").key_pem)

    mail = generate_certificate("mail")
    for key, value in {
        "mail_transport": "graph",
        "graph_tenant": "msp.onmicrosoft.com",
        "graph_client_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "graph_sender": "reports@msp.example",
        "graph_auth": "certificate",
        "graph_cert_pem": mail.cert_pem,
        "graph_key_enc": vault.encrypt(mail.key_pem),
        "graph_thumbprint": mail.thumbprint,
        "graph_cert_not_after": mail.not_after.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "graph_secret_enc": vault.encrypt(MAIL_SECRET),
        "smtp_host": "smtp.example.net",
        "smtp_port": "587",
        "smtp_security": "starttls",
        "smtp_user": "relay",
        "smtp_password_enc": vault.encrypt(SMTP_PASSWORD),
    }.items():
        _setting(db, key, value)
    secrets |= _key("mail key", mail.key_pem) | {"mail secret": MAIL_SECRET, "SMTP password": SMTP_PASSWORD}

    sign_in = sso.new_certificate(db, vault)
    sso.store_manual(
        db, vault, "12345678-1234-1234-1234-123456789012", "87654321-4321-4321-4321-210987654321", "commercial", "", "x"
    )
    secrets |= _key("sign-in app key", sign_in.key_pem)
    secrets |= _key("sign-in app's next key", sso.new_certificate(db, vault, replace=True).key_pem)

    alerts.save_settings(
        db,
        {
            "alerts.enabled": "1",
            "alerts.url_enc": vault.encrypt(f"https://hooks.example.net/notify/{WEBHOOK_KEY}"),
            "alerts.secret_enc": vault.encrypt(SIGNING_SECRET),
            "alerts.system": "1",
        },
    )
    secrets |= {"webhook key": WEBHOOK_KEY, "signing secret": SIGNING_SECRET}

    secrets["monitoring token"] = system_health.new_token(db).removeprefix("osh_")
    secrets["monitoring token's hash"] = db.execute(
        "SELECT value FROM settings WHERE key = ?", (system_health.TOKEN_KEY,)
    ).fetchone()[0]

    admin = make_user(db, "admin")
    now = utcnow()
    for purpose in ("connection", "sign_in"):
        db.execute(
            "INSERT INTO setup_sign_ins (user_id, cloud, tenant, name, device_code_enc, user_code, verification_uri,"
            " poll_seconds, expires_at, status, created_at, updated_at, purpose) VALUES (?, 'commercial',"
            " 'organizations', 'App', ?, 'ABCD-EFGH', 'https://microsoft.com/devicelogin', 5,"
            " '2999-01-01T00:00:00Z', 'waiting', ?, ?, ?)",
            (admin, vault.encrypt(DEVICE_CODE), now, now, purpose),
        )
    secrets["device code"] = DEVICE_CODE

    # Someone else, with two-step sign-in and a recovery code, signed in in another browser.
    other = make_user(db, "other", role="analyst", all_tenants=True)
    browser = app.test_client()
    assert login(browser, "other").status_code == 302
    with browser.session_transaction() as session:
        secrets["their session token"] = session["sid"]
    secrets["their session's hash"] = db.execute(
        "SELECT token_hash FROM sessions WHERE user_id = ?", (other,)
    ).fetchone()[0]
    db.execute(
        "UPDATE users SET totp_secret_enc = ?, totp_pending_enc = ?, totp_enabled_at = ? WHERE id = ?",
        (vault.encrypt(OTHER_TOTP), vault.encrypt(OTHER_PENDING_TOTP), now, other),
    )
    recovery_key = totp.recovery_key(db, vault)
    code_hash = totp.hash_recovery_code("abcde-fghjk", recovery_key)
    db.execute("INSERT INTO user_recovery_codes (user_id, code_hash) VALUES (?, ?)", (other, code_hash))
    secrets |= {
        "their two-step secret": OTHER_TOTP,
        "their two-step secret, grouped": totp.grouped(OTHER_TOTP),
        "their pending two-step secret": OTHER_PENDING_TOTP,
        "a recovery code's hash": code_hash.removeprefix("hmac:"),
        "the recovery code key": recovery_key.decode(),
        "a password hash": PASSWORD_HASH.split("$", 1)[1],
        "a password": PASSWORD,
        "the session key": cfg.secret_key,
        "the encryption key": cfg.encryption_keys[0],
    }

    # Every encrypted value as stored.
    for table, column in (
        ("app_connections", "key_enc"),
        ("app_connections", "next_key_enc"),
        ("users", "totp_secret_enc"),
        ("users", "totp_pending_enc"),
        ("setup_sign_ins", "device_code_enc"),
    ):
        for (value,) in db.execute(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL AND {column} != ''"):
            secrets[f"{table}.{column} as stored ({value[-8:]})"] = value[-40:]
    for key, value in db.execute(
        "SELECT key, value FROM settings WHERE key LIKE '%\\_enc' ESCAPE '\\' AND value != ''"
    ):
        secrets[f"settings {key} as stored"] = value[-40:]
    return alpha, bravo, ids, secrets


def _carries(data: bytes, secrets: dict[str, str | bytes]) -> list[str]:
    return [
        label for label, secret in secrets.items() if (secret if isinstance(secret, bytes) else secret.encode()) in data
    ]


def _found(resp, secrets: dict[str, str | bytes]) -> list[str]:
    """Which secrets the answer carries: in its body, its headers or any file inside it."""
    body = resp.get_data()
    parts = [("body", body), ("headers", str(resp.headers).encode())]
    if body[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            parts += [(name, z.read(name)) for name in z.namelist()]
    return [f"{label} in {where}" for where, data in parts for label in _carries(data, secrets)]


def _urls(app, db, alpha: int, ids: dict):
    """The isolation walk's GET routes with real ids (every download in every format), and the certificates
    waiting to replace the current ones."""
    for _, url in _a_urls(app, db, alpha, ids):
        yield url
        if url.endswith("certificate.cer"):
            yield url + "?which=next"


@pytest.mark.parametrize("who", ["admin", "analyst", "client"])
def test_no_secret_on_any_page_or_download(app, client, db, cfg, planted, _pdf_as_page, who):  # noqa: F811
    alpha, bravo, ids, secrets = planted
    if who != "admin":
        make_user(db, who, role=who, tenants=[alpha, bravo])
    assert login(client, who).status_code == 302
    with client.session_transaction() as session:
        secrets = {**secrets, "their own session token": session["sid"]}
    leaks, opened = [], 0
    for url in _urls(app, db, alpha, ids):
        resp = client.get(url)
        opened += resp.status_code == 200
        if found := _found(resp, secrets):
            leaks.append((url, resp.status_code, found))
    for process in logs.PROCESSES:
        leaks += [
            (f"{process} log", 0, label)
            for label in _carries(logs.tail(cfg.data_dir, process, 10**7).encode(), secrets)
        ]
    assert opened >= {"admin": 270, "analyst": 230, "client": 60}[who], opened
    assert not leaks, "\n".join(map(str, leaks))


def test_the_crawl_sees_the_planted_settings(client, db, vault, planted):
    """The search finds a key wherever one is, and the pages that would show a secret open with the setting
    around it, so the crawl isn't vacuous."""
    _, _, ids, secrets = planted
    pem = vault.decrypt(store.get_connection(db, ids["connection_id"])["key_enc"])
    der = base64.b64decode("".join(pem.strip().splitlines()[1:-1]))
    assert _found(Response(pem), secrets) == ["app connection key (PEM) in body"]
    assert _found(Response(der), secrets) == ["app connection key (DER) in body"]
    login(client, "admin")
    assert b"ABCD-EFGH" in client.get("/admin/connections").data
    assert b"reports@msp.example" in client.get("/admin/email").data
    assert b"12345678-1234-1234-1234-123456789012" in client.get("/admin/sign-in").data
    assert alerts.masked_url(vault, alerts.settings(db)).encode() in client.get("/admin/alerts").data
    for url in (
        f"/admin/connections/{ids['connection_id']}/certificate.cer?which=next",
        "/admin/email/mail-certificate.cer",
        "/admin/sign-in/certificate.cer?which=next",
    ):
        resp = client.get(url)
        assert resp.status_code == 200 and x509.load_der_x509_certificate(resp.data), url  # the public half only
