"""App-only tokens from the connection's certificate, for Graph and Exchange."""

from __future__ import annotations

from collections.abc import Callable


class TokenError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


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
