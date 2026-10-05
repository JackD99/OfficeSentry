"""App-only tokens from the connection's certificate, for Graph and Exchange."""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Callable


class TokenError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def secret_token_provider(tenant_ref: str, client_id: str, secret: str, resource: str) -> Callable[[], str]:
    """Client-secret variant, used only by the ``live-test`` command against a test tenant."""
    import msal

    app = msal.ConfidentialClientApplication(
        client_id=client_id, authority=f"https://login.microsoftonline.com/{tenant_ref}", client_credential=secret)

    def token() -> str:
        result = app.acquire_token_for_client(scopes=[f"{resource}/.default"])
        if "access_token" not in result:
            raise TokenError(result.get("error", "auth_failed"),
                             result.get("error_description", "token request failed"))
        return result["access_token"]

    return token


def certificate_token_provider(tenant_ref: str, client_id: str, key_pem: str, thumbprint: str,
                               resource: str) -> Callable[[], str]:
    """Return a callable giving a token for ``resource`` (e.g. https://graph.microsoft.com).

    MSAL caches the token in memory and renews it before expiry.
    """
    import msal

    app = msal.ConfidentialClientApplication(
        client_id=client_id,
        authority=f"https://login.microsoftonline.com/{tenant_ref}",
        client_credential={"private_key": key_pem, "thumbprint": thumbprint},
    )

    def token() -> str:
        result = app.acquire_token_for_client(scopes=[f"{resource}/.default"])
        if "access_token" not in result:
            raise TokenError(result.get("error", "auth_failed"),
                             result.get("error_description", "token request failed"))
        return result["access_token"]

    return token


# Token providers kept between runs, so a tenant's nightly runs sign in once per
# resource rather than once each. Least recently used first; bounded so a
# worker serving many tenants doesn't keep every key in memory.
_PROVIDERS: OrderedDict[tuple, Callable[[], str]] = OrderedDict()
_PROVIDERS_LOCK = threading.Lock()
MAX_PROVIDERS = 256


def shared_certificate_token_provider(tenant_ref: str, client_id: str, key_pem: str, thumbprint: str,
                                      resource: str) -> Callable[[], str]:
    """``certificate_token_provider``, reused across runs for the same tenant, app, certificate and resource.

    MSAL renews the cached token before it expires and is safe to share
    between threads. A new certificate or key is a new entry. Connection
    checks use ``certificate_token_provider`` so they always sign in afresh.
    """
    key = (tenant_ref.lower(), client_id, thumbprint, hashlib.sha256(key_pem.encode()).hexdigest(), resource)
    with _PROVIDERS_LOCK:
        provider = _PROVIDERS.get(key)
        if provider is not None:
            _PROVIDERS.move_to_end(key)
            return provider
    provider = certificate_token_provider(tenant_ref, client_id, key_pem, thumbprint, resource)
    with _PROVIDERS_LOCK:
        provider = _PROVIDERS.setdefault(key, provider)
        _PROVIDERS.move_to_end(key)
        while len(_PROVIDERS) > MAX_PROVIDERS:
            _PROVIDERS.popitem(last=False)
    return provider
