"""App-only tokens from the connection's certificate, for Graph and Exchange."""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Callable

from . import clouds


class TokenError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def authority(tenant_ref: str, cloud: str | None = None) -> str:
    """The sign-in authority for a tenant in its cloud (``https://login.microsoftonline.us/<tenant>``...)."""
    return f"{clouds.get(cloud).login}/{tenant_ref}"


def _check_resource(resource: str, cloud: str | None) -> None:
    """A token for one cloud's sign-in must be for that same cloud's Graph or Exchange."""
    c = clouds.get(cloud)
    if resource not in (c.graph, c.exchange):
        raise ValueError(f"{resource} isn't a {c.label} resource")


def secret_token_provider(
    tenant_ref: str, client_id: str, secret: str, resource: str, cloud: str | None = None
) -> Callable[[], str]:
    """Client-secret variant, used by the ``live-test`` command and the MSP's own mail app."""
    import msal

    _check_resource(resource, cloud)
    app = msal.ConfidentialClientApplication(
        client_id=client_id, authority=authority(tenant_ref, cloud), client_credential=secret
    )

    def token() -> str:
        result = app.acquire_token_for_client(scopes=[f"{resource}/.default"])
        if "access_token" not in result:
            raise TokenError(
                result.get("error", "auth_failed"), result.get("error_description", "token request failed")
            )
        return result["access_token"]

    return token


def federated_token_provider(
    tenant_ref: str, client_id: str, assertion: Callable[[], str], resource: str, cloud: str | None = None
) -> Callable[[], str]:
    """Federated-credential variant: ``assertion`` returns a token another identity provider issued (GitHub
    Actions' OIDC token, for the live canary), which the app trusts instead of holding a secret."""
    import msal

    _check_resource(resource, cloud)
    app = msal.ConfidentialClientApplication(
        client_id=client_id, authority=authority(tenant_ref, cloud), client_credential={"client_assertion": assertion}
    )

    def token() -> str:
        result = app.acquire_token_for_client(scopes=[f"{resource}/.default"])
        if "access_token" not in result:
            raise TokenError(
                result.get("error", "auth_failed"), result.get("error_description", "token request failed")
            )
        return result["access_token"]

    return token


def certificate_token_provider(
    tenant_ref: str, client_id: str, key_pem: str, thumbprint: str, resource: str, cloud: str | None = None
) -> Callable[[], str]:
    """Return a callable giving a token for ``resource`` (e.g. https://graph.microsoft.com).

    ``cloud`` picks the sign-in host (``clouds.py``); ``resource`` must be that same
    cloud's Graph or Exchange root. MSAL caches the token in memory and renews it before expiry.
    """
    import msal

    _check_resource(resource, cloud)
    app = msal.ConfidentialClientApplication(
        client_id=client_id,
        authority=authority(tenant_ref, cloud),
        client_credential={"private_key": key_pem, "thumbprint": thumbprint},
    )

    def token() -> str:
        result = app.acquire_token_for_client(scopes=[f"{resource}/.default"])
        if "access_token" not in result:
            raise TokenError(
                result.get("error", "auth_failed"), result.get("error_description", "token request failed")
            )
        return result["access_token"]

    return token


# Token providers kept between runs, so a tenant's nightly runs sign in once per
# resource rather than once each. Least recently used first; bounded so a
# worker serving many tenants doesn't keep every key in memory.
_PROVIDERS: OrderedDict[tuple, Callable[[], str]] = OrderedDict()
_PROVIDERS_LOCK = threading.Lock()
MAX_PROVIDERS = 256


def shared_certificate_token_provider(
    tenant_ref: str, client_id: str, key_pem: str, thumbprint: str, resource: str, cloud: str | None = None
) -> Callable[[], str]:
    """``certificate_token_provider``, reused across runs for the same tenant, app, certificate and resource.

    MSAL renews the cached token before it expires and is safe to share
    between threads. A new certificate or key is a new entry. Connection
    checks use ``certificate_token_provider`` so they always sign in afresh.
    """
    key = (
        tenant_ref.lower(),
        client_id,
        thumbprint,
        hashlib.sha256(key_pem.encode()).hexdigest(),
        resource,
        clouds.get(cloud).key,
    )
    with _PROVIDERS_LOCK:
        provider = _PROVIDERS.get(key)
        if provider is not None:
            _PROVIDERS.move_to_end(key)
            return provider
    provider = certificate_token_provider(tenant_ref, client_id, key_pem, thumbprint, resource, cloud)
    with _PROVIDERS_LOCK:
        provider = _PROVIDERS.setdefault(key, provider)
        _PROVIDERS.move_to_end(key)
        while len(_PROVIDERS) > MAX_PROVIDERS:
            _PROVIDERS.popitem(last=False)
    return provider
