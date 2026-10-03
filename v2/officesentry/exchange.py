"""Exchange Online admin API client: app-only and read-only.

Exchange data Graph doesn't expose (mailbox forwarding, mailbox permissions,
shared mailboxes, org-wide mail settings) comes from the same REST endpoint
the ExchangeOnlineManagement module uses:

    POST https://outlook.office365.com/adminapi/beta/{tenant}/InvokeCommand
    {"CmdletInput": {"CmdletName": "Get-Mailbox", "Parameters": {...}}}

It is a POST, so read-only is enforced here instead: only the cmdlets in
``READ_ONLY_CMDLETS`` can be called. Never add a cmdlet that isn't ``Get-*``.

The app needs the ``Exchange.ManageAsApp`` permission and, in each tenant,
the Global Reader role assigned to its service principal.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import requests

log = logging.getLogger(__name__)

EXCHANGE_ROOT = "https://outlook.office365.com"
RETRY_STATUSES = {429, 500, 502, 503, 504}

READ_ONLY_CMDLETS = frozenset({
    "Get-Mailbox",
    "Get-MailboxPermission",
    "Get-MailboxStatistics",
    "Get-CASMailbox",
    "Get-Recipient",
    "Get-RecipientPermission",
    "Get-OrganizationConfig",
    "Get-TransportConfig",
    "Get-RemoteDomain",
    "Get-HostedOutboundSpamFilterPolicy",
    "Get-AcceptedDomain",
})

ACCESS_HELP = ("Exchange refused access. In this tenant, assign the Global Reader role to the "
               "Office Sentry app (Entra admin centre > Roles and admins > Global Reader > Add "
               "assignments), and make sure admin consent includes Exchange.ManageAsApp.")


class ExchangeError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"Exchange {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class ExchangeAccessDenied(ExchangeError):
    """No Exchange.ManageAsApp consent, or no Exchange role for the app in this tenant."""


def _parse_error(resp: requests.Response) -> ExchangeError:
    code, message = "unknown", resp.text[:300] or resp.reason or ""
    try:
        err = resp.json().get("error", {})
        code = err.get("code", code)
        details = err.get("details") or []
        message = (details[0].get("message") if details else None) or err.get("message", message)
    except (ValueError, AttributeError):
        pass
    if resp.status_code in (401, 403):
        return ExchangeAccessDenied(resp.status_code, code, f"{message} {ACCESS_HELP}".strip())
    return ExchangeError(resp.status_code, code, message)


class ExchangeClient:
    def __init__(
        self,
        tenant_id: str,
        token_provider: Callable[[], str],
        *,
        session: requests.Session | None = None,
        max_retries: int = 6,
        timeout: int = 120,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.tenant_id = tenant_id
        self._token_provider = token_provider
        self._session = session or requests.Session()
        self._max_retries = max_retries
        self._timeout = timeout
        self._sleep = sleep

    @classmethod
    def from_certificate(cls, tenant_ref: str, client_id: str, key_pem: str,
                         thumbprint: str, **kwargs) -> ExchangeClient:
        from .msauth import TokenError, certificate_token_provider

        provider = certificate_token_provider(tenant_ref, client_id, key_pem, thumbprint, EXCHANGE_ROOT)

        def token() -> str:
            try:
                return provider()
            except TokenError as exc:
                raise ExchangeError(401, exc.code, exc.message) from exc

        return cls(tenant_ref, token, **kwargs)

    @property
    def url(self) -> str:
        return f"{EXCHANGE_ROOT}/adminapi/beta/{self.tenant_id}/InvokeCommand"

    def _post(self, url: str, body: dict) -> dict:
        attempt = 0
        while True:
            headers = {
                "Authorization": f"Bearer {self._token_provider()}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-AnchorMailbox": f"APP:SystemMailbox{{bb558c35-97f1-4cb9-8ff7-d53741dc928c}}@{self.tenant_id}",
                "Prefer": "odata.maxpagesize=1000",
            }
            try:
                resp = self._session.post(url, json=body, headers=headers, timeout=self._timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= self._max_retries:
                    raise ExchangeError(0, "network_error", str(exc)) from exc
                self._sleep(min(2 ** attempt, 60))
                attempt += 1
                continue
            if resp.status_code in RETRY_STATUSES and attempt < self._max_retries:
                value = resp.headers.get("Retry-After", "")
                delay = min(float(value), 300) if value.isdigit() else float(min(2 ** attempt, 60))
                log.info("Exchange %s; retrying in %ss", resp.status_code, delay)
                self._sleep(delay)
                attempt += 1
                continue
            if not resp.ok:
                raise _parse_error(resp)
            return resp.json() if resp.content else {}

    def invoke(self, cmdlet: str, parameters: dict | None = None, *, max_pages: int = 200) -> list[dict]:
        """Run a read-only cmdlet and return every result, following paging."""
        if cmdlet not in READ_ONLY_CMDLETS:
            raise ValueError(f"{cmdlet} is not an allowed read-only Exchange cmdlet")
        body = {"CmdletInput": {"CmdletName": cmdlet, "Parameters": parameters or {}}}
        url: str | None = self.url
        items: list[dict] = []
        pages = 0
        while url:
            if not url.startswith(f"{EXCHANGE_ROOT}/adminapi/"):
                raise ValueError(f"refusing to send an Exchange token to {url.split('/')[2]}")
            if pages >= max_pages:
                raise ExchangeError(0, "too_many_pages", f"{cmdlet} returned more than {max_pages} pages")
            data = self._post(url, body)
            items.extend(data.get("value", []))
            pages += 1
            url = data.get("@odata.nextLink")
        return items
