"""Exchange Online admin API client: app-only and read-only.

Exchange data Graph doesn't expose (mailbox forwarding, mailbox permissions,
shared mailboxes, org-wide mail settings) comes from the same REST endpoint
the ExchangeOnlineManagement module uses:

    POST https://outlook.office365.com/adminapi/beta/{tenant}/InvokeCommand

(or the tenant's national cloud's Exchange host, see ``clouds.py``)
    {"CmdletInput": {"CmdletName": "Get-Mailbox", "Parameters": {...}}}

It is a POST, so read-only is enforced here instead: only the cmdlets in
``READ_ONLY_CMDLETS`` can be called. Never add a cmdlet that isn't ``Get-*``.

The app needs the ``Exchange.ManageAsApp`` permission and, in each tenant,
the Global Reader role assigned to its service principal.

With the worker's ``limiter``, a 429 pauses the tenant's other Exchange calls
too (``ratelimit.py``); Microsoft documents no rate to pace them at. Every
request is counted in ``telemetry`` under its cmdlet.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import requests

from . import clouds, endpoints
from .graph import USER_AGENT
from .ratelimit import Limiter, Telemetry, claims, to_pause

log = logging.getLogger(__name__)

EXCHANGE_ROOT = clouds.get(clouds.DEFAULT).exchange  # commercial; a client for another cloud uses its own
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_PAGES = 200  # pages of one cmdlet's answer (Exchange sends up to 1,000 results a page) before it's cut

READ_ONLY_CMDLETS = frozenset(
    {
        "Get-Mailbox",
        "Get-MailboxPermission",
        "Get-MailboxStatistics",
        "Get-CASMailbox",
        "Get-InboxRule",
        "Get-Recipient",
        "Get-RecipientPermission",
        "Get-OrganizationConfig",
        "Get-AdminAuditLogConfig",
        "Get-TransportConfig",
        "Get-RemoteDomain",
        "Get-HostedOutboundSpamFilterPolicy",
        "Get-AcceptedDomain",
        "Get-MobileDevice",
        "Get-MobileDeviceStatistics",
        "Get-DistributionGroup",
        "Get-DistributionGroupMember",
        "Get-MessageTraceV2",
        "Get-MailTrafficSummaryReport",
        "Get-TransportRule",
        "Get-InboundConnector",
        "Get-OutboundConnector",
        "Get-HostedContentFilterPolicy",
        "Get-HostedContentFilterRule",
        "Get-AntiPhishPolicy",
        "Get-AntiPhishRule",
        "Get-MalwareFilterPolicy",
        "Get-MalwareFilterRule",
        "Get-SafeLinksPolicy",
        "Get-SafeLinksRule",
        "Get-SafeAttachmentPolicy",
        "Get-SafeAttachmentRule",
        "Get-EOPProtectionPolicyRule",
        "Get-ATPProtectionPolicyRule",
    }
)

ACCESS_HELP = (
    "Exchange refused access. In this tenant, assign the Global Reader role to the "
    "Office Sentry app (Entra admin centre > Roles and admins > Global Reader > Add "
    "assignments), and make sure admin consent includes Exchange.ManageAsApp."
)


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
        cloud: str | None = None,
        limiter: Limiter | None = None,
        telemetry: Telemetry | None = None,
        app: str = "",
    ):
        self.cloud = clouds.get(cloud)
        self.root = self.cloud.exchange
        self.limiter = limiter
        self.telemetry = telemetry or Telemetry()
        self.app = app
        self.tenant_id = tenant_id
        self._token_provider = token_provider
        self._session = session or requests.Session()
        self._max_retries = max_retries
        self._timeout = timeout
        self._sleep = sleep
        self.truncated: dict[str, int] = {}  # cmdlets whose answer was cut at the page cap, and the cap

    @classmethod
    def from_certificate(
        cls, tenant_ref: str, client_id: str, key_pem: str, thumbprint: str, cloud: str | None = None, **kwargs
    ) -> ExchangeClient:
        from .msauth import TokenError, shared_certificate_token_provider

        root = clouds.get(cloud).exchange
        provider = shared_certificate_token_provider(tenant_ref, client_id, key_pem, thumbprint, root, cloud)

        def token() -> str:
            try:
                return provider()
            except TokenError as exc:
                # Not reaching Microsoft is a network failure like any other, not a refused sign-in.
                raise ExchangeError(0 if exc.code == "network_error" else 401, exc.code, exc.message) from exc

        return cls(tenant_ref, token, cloud=cloud, app=client_id, **kwargs)

    @property
    def url(self) -> str:
        return f"{self.root}/adminapi/beta/{self.tenant_id}/InvokeCommand"

    def _post(self, url: str, body: dict) -> dict:
        cmdlet = body["CmdletInput"]["CmdletName"]
        endpoint = endpoints.exchange(cmdlet)
        key = f"Exchange {cmdlet}"
        taken = claims(endpoint, self.app, self.tenant_id, graph=False) if self.limiter else []
        attempt = 0
        while True:
            waited = self.limiter.acquire(taken) if self.limiter else 0.0
            headers = {
                "User-Agent": USER_AGENT,
                "Authorization": f"Bearer {self._token_provider()}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-AnchorMailbox": f"APP:SystemMailbox{{bb558c35-97f1-4cb9-8ff7-d53741dc928c}}@{self.tenant_id}",
                "Prefer": "odata.maxpagesize=1000",
            }
            try:
                resp = self._session.post(url, json=body, headers=headers, timeout=self._timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                retry = attempt < self._max_retries
                delay = float(min(2**attempt, 60)) if retry else 0.0
                self.telemetry.request(key, endpoint, status=0, limiter_wait=waited, retry_wait=delay)
                if not retry:
                    raise ExchangeError(0, "network_error", str(exc)) from exc
                self._sleep(delay)
                attempt += 1
                continue
            retry = resp.status_code in RETRY_STATUSES and attempt < self._max_retries
            delay = 0.0
            if retry:
                value = resp.headers.get("Retry-After", "")
                delay = min(float(value), 300) if value.isdigit() else float(min(2**attempt, 60))
            self.telemetry.request(
                key, endpoint, status=resp.status_code, limiter_wait=waited, retry_wait=delay, headers=resp.headers
            )
            if retry:
                log.info("Exchange %s; retrying in %ss", resp.status_code, delay)
                if resp.status_code == 429 and self.limiter:
                    self.limiter.pause(to_pause(taken), delay)
                self._sleep(delay)
                attempt += 1
                continue
            if not resp.ok:
                raise _parse_error(resp)
            return resp.json() if resp.content else {}

    def invoke(self, cmdlet: str, parameters: dict | None = None, *, max_pages: int | None = None) -> list[dict]:
        """Run a read-only cmdlet and return every result, following paging. An answer longer than
        ``max_pages`` (``MAX_PAGES``) pages is cut there and noted in ``truncated`` (the run becomes partial
        and says so), rather than failing the whole collection over one huge list."""
        if cmdlet not in READ_ONLY_CMDLETS:
            raise ValueError(f"{cmdlet} is not an allowed read-only Exchange cmdlet")
        max_pages = MAX_PAGES if max_pages is None else max_pages
        body = {"CmdletInput": {"CmdletName": cmdlet, "Parameters": parameters or {}}}
        url: str | None = self.url
        items: list[dict] = []
        pages = 0
        while url:
            if not url.startswith(f"{self.root}/adminapi/"):
                raise ValueError(f"refusing to send an Exchange token to {url.split('/')[2]}")
            if pages >= max_pages:
                log.warning("Stopped paging %s after %s pages", cmdlet, max_pages)
                self.truncated[cmdlet] = max_pages
                break
            data = self._post(url, body)
            items.extend(data.get("value", []))
            pages += 1
            url = data.get("@odata.nextLink")
        return items
