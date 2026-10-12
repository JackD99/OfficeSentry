"""Sending email: SMTP, or Microsoft Graph ``sendMail`` from the MSP's own mailbox.

This module only moves a finished message. What gets sent, to whom and when
lives in ``delivery.py``.

Graph sending uses a *separate* single-tenant app registration in the MSP's
own tenant with the ``Mail.Send`` application permission. It is never the
multi-tenant read-only app, so client tenants are never asked to consent to
mail permissions, and it is the only Graph call in Office Sentry that is not a
GET.

The mail app signs in with a certificate Office Sentry makes (recommended: the
private key never leaves it) or a client secret. ``Mail.Send`` lets an app send
as *anyone* in the tenant until Exchange limits it to one mailbox, so the
settings page treats that limit as a setup step the admin confirms
(``graph_scoped_at``) rather than a footnote. The mail app is in the MSP's own
Microsoft cloud (``graph_cloud``), which needn't be its clients'.
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import ipaddress
import logging
import re
import smtplib
import ssl
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

import requests

from . import clouds, outbound
from .text import nouns

EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
MAX_RECIPIENTS = 20
GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")

TRANSPORTS = {"smtp": "SMTP server", "graph": "Microsoft 365 (Graph)"}
GRAPH_AUTH = {"certificate": "Certificate", "secret": "Client secret"}
SMTP_KEYS = ("smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_password_enc", "smtp_from")
GRAPH_KEYS = ("graph_tenant", "graph_client_id", "graph_secret_enc", "graph_sender")
# The mail app's certificate (made by Office Sentry; only the public part is ever downloaded), its cloud,
# when the admin confirmed it's limited to one mailbox, and the last expiry warning sent for it.
GRAPH_CERT_KEYS = (
    "graph_auth",
    "graph_cert_pem",
    "graph_key_enc",
    "graph_thumbprint",
    "graph_cert_not_after",
    "graph_cloud",
    "graph_scoped_at",
    "graph_scoped_by",
    "graph_cert_warned",
)
SETTING_KEYS = ("mail_transport", "msp_name", *SMTP_KEYS, *GRAPH_KEYS, *GRAPH_CERT_KEYS)
ENCRYPTED_KEYS = ("smtp_password_enc", "graph_secret_enc", "graph_key_enc")

# Graph rejects JSON requests over 4 MB; attachments grow by a third as base64.
GRAPH_MAX_ATTACHMENT_BYTES = 2_900_000

# Exchange Online takes 30 messages a minute from one mailbox and answers 429 past that, by SMTP or Graph.
# A month-end approving 200 clients at once sent faster than that and lost a fifth of its emails, so every
# send in a process takes its turn (``PACER``), and background work (``patient``) waits out 429 and 503
# answers too. A page never waits: it sends at once, as before.
PER_MINUTE = 30
BACKGROUND_PATIENCE = 600  # seconds a background send may wait for its turn
RETRY_STATUSES = (429, 503)  # Microsoft hadn't taken the message, so sending it again can't send it twice
DEFAULT_RETRY_AFTER = 30

log = logging.getLogger(__name__)


class MailError(Exception):
    """Sending failed. The message is written for the person reading the delivery log."""


class MailNotConfigured(MailError):
    pass


@dataclass
class Email:
    to: list[str]
    subject: str
    text: str
    html: str | None = None
    bcc: list[str] = field(default_factory=list)
    attachments: list[tuple[str, str, bytes]] = field(default_factory=list)  # (filename, mimetype, data)
    # Images shown in the HTML (the MSP's logo): (content id, mimetype, data), referenced as src="cid:<id>".
    inline: list[tuple[str, str, bytes]] = field(default_factory=list)

    def html_for_browser(self) -> str:
        """The HTML with inline images as data: URIs, for previewing in a browser (which can't resolve cid:)."""
        html = self.html or ""
        for cid, mimetype, data in self.inline:
            html = html.replace(f"cid:{cid}", f"data:{mimetype};base64,{base64.b64encode(data).decode()}")
        return html


def parse_recipients(raw: str) -> list[str] | None:
    """Split a comma/semicolon/newline separated list; None if any address is invalid."""
    addrs = [a.strip() for a in re.split(r"[,;\s]+", raw or "") if a.strip()]
    if not addrs or len(addrs) > MAX_RECIPIENTS or not all(EMAIL_RE.match(a) for a in addrs):
        return None
    return list(dict.fromkeys(addrs))


def valid_email(addr: str) -> bool:
    return bool(EMAIL_RE.match(addr or ""))


def mail_settings(conn) -> dict:
    rows = conn.execute(
        f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(SETTING_KEYS))})", SETTING_KEYS
    ).fetchall()
    return {r["key"]: r["value"] for r in rows}


def transport(settings: dict) -> str:
    return settings.get("mail_transport") or "smtp"


def graph_auth(settings: dict) -> str:
    """How the mail app signs in. Installs set up before certificates were offered use a secret."""
    return settings.get("graph_auth") or "secret"


def graph_cloud(settings: dict) -> clouds.Cloud:
    return clouds.get(settings.get("graph_cloud"))


def is_configured(settings: dict) -> bool:
    if transport(settings) == "graph":
        credential = (
            ("graph_key_enc", "graph_thumbprint") if graph_auth(settings) == "certificate" else ("graph_secret_enc",)
        )
        return all(settings.get(k) for k in ("graph_tenant", "graph_client_id", "graph_sender", *credential))
    return bool(settings.get("smtp_host") and settings.get("smtp_from"))


def scoped(settings: dict) -> bool:
    """Whether the admin confirmed the mail app can only send as the one mailbox."""
    return bool(settings.get("graph_scoped_at"))


def send_url(settings: dict) -> str:
    sender = requests.utils.quote(settings["graph_sender"], safe="@")
    return f"{graph_cloud(settings).graph}/v1.0/users/{sender}/sendMail"


def sender_label(settings: dict) -> str:
    """The From address people will see, for the settings page and previews."""
    if transport(settings) == "graph":
        return settings.get("graph_sender") or ""
    return settings.get("smtp_from") or ""


class Pacer:
    """At most ``per_minute`` messages in any minute from each sender, shared by every thread in the process,
    and no message from a sender Microsoft has asked to wait until that wait is over."""

    def __init__(self, per_minute: int = PER_MINUTE, clock=time.monotonic, sleep=time.sleep):
        self.per_minute, self.clock, self.sleep = per_minute, clock, sleep
        self._lock = threading.Lock()
        self._sent: dict[str, deque] = {}
        self._held: dict[str, float] = {}
        self._refusing: dict[str, float] = {}  # since when Microsoft has refused every message from the sender

    def turn(self, sender: str, deadline: float) -> None:
        """Wait until ``sender`` may send again, then count the message. Past ``deadline`` it stops waiting
        and the message goes anyway, so Microsoft decides."""
        key = sender.lower()
        while True:
            with self._lock:
                now = self.clock()
                sent = self._sent.setdefault(key, deque())
                while sent and sent[0] <= now - 60:
                    sent.popleft()
                ready = max(self._held.get(key, 0.0), sent[0] + 60 if len(sent) >= self.per_minute else 0.0)
                if ready <= now or ready > deadline:
                    sent.append(now)
                    return
            self.sleep(ready - now)

    def refused(self, sender: str, seconds: float) -> None:
        """Microsoft asked this sender to wait: every thread waits with it."""
        key = sender.lower()
        with self._lock:
            now = self.clock()
            self._held[key] = max(self._held.get(key, 0.0), now + seconds)
            self._refusing.setdefault(key, now)

    def accepted(self, sender: str) -> None:
        with self._lock:
            self._refusing.pop(sender.lower(), None)

    def refusing_for(self, sender: str) -> float:
        """Seconds Microsoft has refused every message from ``sender`` (0 when it last took one)."""
        with self._lock:
            since = self._refusing.get(sender.lower())
            return self.clock() - since if since is not None else 0.0


PACER = Pacer()
_patience: contextvars.ContextVar[float] = contextvars.ContextVar("mail_patience", default=0.0)


@contextmanager
def patient(seconds: float = BACKGROUND_PATIENCE):
    """Sends inside this block may wait up to ``seconds`` for their turn and for Microsoft's throttling.
    Background tasks use it; a web request mustn't, as it would hold the page."""
    token = _patience.set(seconds)
    try:
        yield
    finally:
        _patience.reset(token)


def send(conn, vault, email: Email) -> str:
    """Send ``email`` with the configured transport. Returns the transport used."""
    s = mail_settings(conn)
    if not s.get("msp_name"):  # the sender's name falls back to the company name on the Branding page
        row = conn.execute("SELECT value FROM settings WHERE key = 'brand_name'").fetchone()
        s["msp_name"] = row["value"] if row else ""
    if not is_configured(s):
        raise MailNotConfigured("Email isn't set up yet (Settings → Email delivery)")
    if not email.to:
        raise MailError("No one to send to")
    deadline = PACER.clock() + _patience.get()
    if transport(s) == "graph":
        _send_graph(s, vault, email, deadline)
        return "graph"
    PACER.turn(sender_label(s), deadline)
    _send_smtp(s, vault, email)
    return "smtp"


# -- SMTP ----------------------------------------------------------------------


def to_mime(email: Email, sender: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(email.to)  # Bcc is never written into the headers
    msg["Subject"] = email.subject
    msg.set_content(email.text)
    if email.html:
        msg.add_alternative(email.html, subtype="html")
        if email.inline:
            html_part = msg.get_body(("html",))
            for cid, mimetype, data in email.inline:
                maintype, subtype = mimetype.split("/", 1)
                html_part.add_related(
                    data,
                    maintype=maintype,
                    subtype=subtype,
                    cid=f"<{cid}>",
                    disposition="inline",
                    filename=f"{cid}.{subtype}",
                )
    for filename, mimetype, data in email.attachments:
        maintype, subtype = mimetype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg


def is_loopback(host: str) -> bool:
    """A mail relay on this same machine, where a password without TLS never crosses a network."""
    host = (host or "").strip().lower().strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _CheckedAddress:
    """Mixed into smtplib's classes: resolves the server once, refuses an address that is never a mail server
    (``outbound.mail_server_address``) and connects to that address, while EHLO, TLS's server name and the
    certificate check keep the host name."""

    def _get_socket(self, host, port, timeout):
        return super()._get_socket(outbound.mail_server_address(host, port), port, timeout)


def _checked(cls):
    return type(cls.__name__, (_CheckedAddress, cls), {})


def _send_smtp(s: dict, vault, email: Email) -> None:
    sender = s["smtp_from"]
    if s.get("msp_name") and "<" not in sender:
        sender = formataddr((s["msp_name"], sender))
    msg = to_mime(email, sender)
    port = int(s.get("smtp_port") or 587)
    security = s.get("smtp_security", "starttls")
    context = ssl.create_default_context()
    try:
        if security == "ssl":
            server = _checked(smtplib.SMTP_SSL)(s["smtp_host"], port, context=context, timeout=60)
        else:
            server = _checked(smtplib.SMTP)(s["smtp_host"], port, timeout=60)
        with server:
            if security == "starttls":
                server.starttls(context=context)
            if s.get("smtp_user") and security == "none" and not is_loopback(s["smtp_host"]):
                raise MailError(
                    "Office Sentry won't send the mail server password unencrypted. Choose STARTTLS "
                    "or SSL/TLS under Email delivery, or remove the username for a relay without sign-in"
                )
            if s.get("smtp_user"):
                password = vault.decrypt(s["smtp_password_enc"]) if s.get("smtp_password_enc") else ""
                server.login(s["smtp_user"], password)
            server.send_message(msg, from_addr=parseaddr(sender)[1], to_addrs=[*email.to, *email.bcc])
    except outbound.LookupFailed as exc:
        raise MailError(f"Couldn't look up the mail server {s['smtp_host']}") from exc
    except outbound.NotPublic as exc:
        raise MailError(
            f"{s['smtp_host']} resolves to {exc.address}, a link-local, multicast or reserved address that is never "
            "a mail server, so Office Sentry didn't connect. A relay on this machine or your network is fine"
        ) from None
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError("The mail server rejected the username or password") from exc
    except smtplib.SMTPRecipientsRefused as exc:
        refused = ", ".join(exc.recipients)
        raise MailError(f"The mail server refused the {nouns(len(exc.recipients), 'recipient')}: {refused}") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"Couldn't send through {s['smtp_host']}: {exc}") from exc


# -- Microsoft Graph -------------------------------------------------------------

_token_providers: dict[str, object] = {}


def _graph_token(s: dict, vault) -> str:
    """App-only token for the MSP's mail app. MSAL keeps it cached until it expires."""
    from .msauth import TokenError, certificate_token_provider, secret_token_provider

    cloud = graph_cloud(s)
    if graph_auth(s) == "certificate":
        key_pem = vault.decrypt(s["graph_key_enc"])
        credential = f"cert|{s['graph_thumbprint']}|{key_pem}"
    else:
        secret = vault.decrypt(s["graph_secret_enc"])
        credential = f"secret|{secret}"
    cache_key = hashlib.sha256(
        f"{cloud.key}|{s['graph_tenant']}|{s['graph_client_id']}|{credential}".encode()
    ).hexdigest()
    provider = _token_providers.get(cache_key)
    if provider is None:
        if graph_auth(s) == "certificate":
            provider = certificate_token_provider(
                s["graph_tenant"], s["graph_client_id"], key_pem, s["graph_thumbprint"], cloud.graph, cloud.key
            )
        else:
            provider = secret_token_provider(s["graph_tenant"], s["graph_client_id"], secret, cloud.graph, cloud.key)
        _token_providers[cache_key] = provider
    try:
        return provider()
    except TokenError as exc:
        what = (
            "certificate (upload the .cer from Email delivery to the mail app)"
            if graph_auth(s) == "certificate"
            else "client secret"
        )
        raise MailError(
            f"Couldn't sign in to Microsoft 365 to send mail ({exc.code}). Check the tenant ID, client ID and {what}."
        ) from exc


def graph_payload(email: Email) -> dict:
    size = sum(len(data) for _, _, data in email.attachments + email.inline)
    if size > GRAPH_MAX_ATTACHMENT_BYTES:
        raise MailError(
            f"The attachments are {size / 1_000_000:.1f} MB, more than Microsoft 365 accepts in one "
            "message (about 3 MB). Turn off spreadsheets, send fewer reports, or use an SMTP server."
        )

    def addr(a):
        return {"emailAddress": {"address": a}}

    message = {
        "subject": email.subject,
        "body": {"contentType": "HTML" if email.html else "Text", "content": email.html or email.text},
        "toRecipients": [addr(a) for a in email.to],
        "bccRecipients": [addr(a) for a in email.bcc],
        "attachments": [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": name,
                "contentType": mimetype,
                "contentBytes": base64.b64encode(data).decode(),
            }
            for name, mimetype, data in email.attachments
        ]
        + [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": f"{cid}.{mimetype.split('/')[-1]}",
                "contentType": mimetype,
                "contentBytes": base64.b64encode(data).decode(),
                "isInline": True,
                "contentId": cid,
            }
            for cid, mimetype, data in (email.inline if email.html else [])
        ],
    }
    return {"message": message, "saveToSentItems": True}


def retry_after(resp) -> float:
    """Seconds Microsoft asked us to wait (``Retry-After``), or a default when it didn't say."""
    try:
        return max(1.0, float(resp.headers.get("Retry-After", "")))
    except ValueError:
        return DEFAULT_RETRY_AFTER


def _send_graph(s: dict, vault, email: Email, deadline: float) -> None:
    payload = graph_payload(email)
    url, sender = send_url(s), s["graph_sender"]
    if PACER.refusing_for(sender) >= BACKGROUND_PATIENCE:
        # An outage: Microsoft has refused this mailbox for as long as any send may wait. Each later send
        # tries once and fails at once, rather than holding a task thread for ten minutes of its own.
        deadline = PACER.clock()
    while True:
        PACER.turn(sender, deadline)
        token = _graph_token(s, vault)
        try:
            resp = requests.post(url, json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=60)
        except requests.RequestException as exc:
            raise MailError(f"Couldn't reach Microsoft 365: {exc}") from exc
        if resp.status_code == 202:
            PACER.accepted(sender)
            return
        if resp.status_code not in RETRY_STATUSES:
            break
        wait = retry_after(resp)
        PACER.refused(sender, wait)
        if PACER.clock() + wait > deadline:
            break
        log.info("Microsoft 365 answered %s to sendMail; sending again in %.0f seconds", resp.status_code, wait)
    try:
        err = resp.json().get("error", {})
        code, message = err.get("code", ""), err.get("message", "")
    except ValueError:
        code, message = "", resp.text[:200]
    if resp.status_code in (401, 403):
        hint = (
            "The mail app needs the Mail.Send application permission with admin consent, "
            "and must be allowed to send as this mailbox."
        )
    elif resp.status_code == 404 or code in ("ErrorInvalidUser", "MailboxNotEnabledForRESTAPI"):
        hint = f"Check that {s['graph_sender']} is a mailbox in your tenant."
    elif resp.status_code == 429:
        hint = f"Microsoft 365 takes at most {PER_MINUTE} messages a minute from one mailbox."
    else:
        hint = ""
    raise MailError(
        " ".join(p for p in (f"Microsoft 365 refused to send ({resp.status_code} {code}).", message[:200], hint) if p)
    )
