"""Sending email: SMTP, or Microsoft Graph ``sendMail`` from the MSP's own mailbox.

This module only moves a finished message. What gets sent, to whom and when
lives in ``delivery.py``.

Graph sending uses a *separate* single-tenant app registration in the MSP's
own tenant with the ``Mail.Send`` application permission. It is never the
multi-tenant read-only app, so client tenants are never asked to consent to
mail permissions, and it is the only Graph call in Office Sentry that is not a
GET.
"""

from __future__ import annotations

import base64
import hashlib
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

import requests

from .text import nouns

EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
MAX_RECIPIENTS = 20
GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")

TRANSPORTS = {"smtp": "SMTP server", "graph": "Microsoft 365 (Graph)"}
SMTP_KEYS = ("smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_password_enc", "smtp_from")
GRAPH_KEYS = ("graph_tenant", "graph_client_id", "graph_secret_enc", "graph_sender")
SETTING_KEYS = ("mail_transport", "msp_name", *SMTP_KEYS, *GRAPH_KEYS)
ENCRYPTED_KEYS = ("smtp_password_enc", "graph_secret_enc")

GRAPH_SEND_URL = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
# Graph rejects JSON requests over 4 MB; attachments grow by a third as base64.
GRAPH_MAX_ATTACHMENT_BYTES = 2_900_000


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


def is_configured(settings: dict) -> bool:
    if transport(settings) == "graph":
        return all(settings.get(k) for k in GRAPH_KEYS)
    return bool(settings.get("smtp_host") and settings.get("smtp_from"))


def sender_label(settings: dict) -> str:
    """The From address people will see, for the settings page and previews."""
    if transport(settings) == "graph":
        return settings.get("graph_sender") or ""
    return settings.get("smtp_from") or ""


def send(conn, vault, email: Email) -> str:
    """Send ``email`` with the configured transport. Returns the transport used."""
    s = mail_settings(conn)
    if not is_configured(s):
        raise MailNotConfigured("Email isn't set up yet (Settings → Email delivery)")
    if not email.to:
        raise MailError("No one to send to")
    if transport(s) == "graph":
        _send_graph(s, vault, email)
        return "graph"
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
                html_part.add_related(data, maintype=maintype, subtype=subtype, cid=f"<{cid}>",
                                      disposition="inline", filename=f"{cid}.{subtype}")
    for filename, mimetype, data in email.attachments:
        maintype, subtype = mimetype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg


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
            server = smtplib.SMTP_SSL(s["smtp_host"], port, context=context, timeout=60)
        else:
            server = smtplib.SMTP(s["smtp_host"], port, timeout=60)
        with server:
            if security == "starttls":
                server.starttls(context=context)
            if s.get("smtp_user"):
                password = vault.decrypt(s["smtp_password_enc"]) if s.get("smtp_password_enc") else ""
                server.login(s["smtp_user"], password)
            server.send_message(msg, from_addr=parseaddr(sender)[1], to_addrs=[*email.to, *email.bcc])
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
    from .msauth import TokenError, secret_token_provider

    secret = vault.decrypt(s["graph_secret_enc"])
    cache_key = hashlib.sha256(f"{s['graph_tenant']}|{s['graph_client_id']}|{secret}".encode()).hexdigest()
    provider = _token_providers.get(cache_key)
    if provider is None:
        provider = secret_token_provider(s["graph_tenant"], s["graph_client_id"], secret,
                                         "https://graph.microsoft.com")
        _token_providers[cache_key] = provider
    try:
        return provider()
    except TokenError as exc:
        raise MailError(f"Couldn't sign in to Microsoft 365 to send mail ({exc.code}). "
                        "Check the tenant ID, client ID and client secret.") from exc


def graph_payload(email: Email) -> dict:
    size = sum(len(data) for _, _, data in email.attachments + email.inline)
    if size > GRAPH_MAX_ATTACHMENT_BYTES:
        raise MailError(f"The attachments are {size / 1_000_000:.1f} MB, more than Microsoft 365 accepts in one "
                        "message (about 3 MB). Turn off spreadsheets, send fewer reports, or use an SMTP server.")

    def addr(a):
        return {"emailAddress": {"address": a}}

    message = {
        "subject": email.subject,
        "body": {"contentType": "HTML" if email.html else "Text", "content": email.html or email.text},
        "toRecipients": [addr(a) for a in email.to],
        "bccRecipients": [addr(a) for a in email.bcc],
        "attachments": [
            {"@odata.type": "#microsoft.graph.fileAttachment", "name": name, "contentType": mimetype,
             "contentBytes": base64.b64encode(data).decode()}
            for name, mimetype, data in email.attachments
        ] + [
            {"@odata.type": "#microsoft.graph.fileAttachment", "name": f"{cid}.{mimetype.split('/')[-1]}",
             "contentType": mimetype, "contentBytes": base64.b64encode(data).decode(), "isInline": True,
             "contentId": cid}
            for cid, mimetype, data in (email.inline if email.html else [])
        ],
    }
    return {"message": message, "saveToSentItems": True}


def _send_graph(s: dict, vault, email: Email) -> None:
    payload = graph_payload(email)
    token = _graph_token(s, vault)
    url = GRAPH_SEND_URL.format(sender=requests.utils.quote(s["graph_sender"], safe="@"))
    try:
        resp = requests.post(url, json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    except requests.RequestException as exc:
        raise MailError(f"Couldn't reach Microsoft 365: {exc}") from exc
    if resp.status_code == 202:
        return
    try:
        err = resp.json().get("error", {})
        code, message = err.get("code", ""), err.get("message", "")
    except ValueError:
        code, message = "", resp.text[:200]
    if resp.status_code in (401, 403):
        hint = ("The mail app needs the Mail.Send application permission with admin consent, "
                "and must be allowed to send as this mailbox.")
    elif resp.status_code == 404 or code in ("ErrorInvalidUser", "MailboxNotEnabledForRESTAPI"):
        hint = f"Check that {s['graph_sender']} is a mailbox in your tenant."
    else:
        hint = ""
    raise MailError(" ".join(p for p in (f"Microsoft 365 refused to send ({resp.status_code} {code}).",
                                         message[:200], hint) if p))
