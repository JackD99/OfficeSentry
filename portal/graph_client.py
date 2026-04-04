"""
portal/graph_client.py — Python Microsoft Graph API client.

Replaces PowerShell Shared.psm1 functions (Get-AuditGraphToken, Invoke-GraphRequest)
with a pure-Python implementation using MSAL + requests.

Certificate-based app-only auth — no user credentials needed.
"""

import logging
from pathlib import Path

import msal
import requests

log = logging.getLogger("portal")

# Microsoft Graph base URL
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Default pagination limit (matches Shared.psm1 max 100 pages)
MAX_PAGES = 100


class GraphAuthError(Exception):
    """Raised when Graph API token acquisition fails."""


class GraphRequestError(Exception):
    """Raised when a Graph API request fails."""

    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class GraphClient:
    """Microsoft Graph API client with certificate-based app-only auth.

    Usage::

        client = GraphClient(
            tenant_id="contoso.onmicrosoft.com",
            client_id="<app-client-id>",
            pfx_path="/path/to/cert.pfx",
            pfx_password="secret",
        )
        domains = client.get_paginated("/domains")
    """

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        pfx_path: str | Path,
        pfx_password: str,
    ):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self._token: str | None = None
        self._session = requests.Session()

        # Load PFX and extract key + cert for MSAL
        self._credential = self._load_pfx(str(pfx_path), pfx_password)

        # Create MSAL confidential client
        self._app = msal.ConfidentialClientApplication(
            client_id=client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            client_credential=self._credential,
        )

    @staticmethod
    def _load_pfx(pfx_path: str, pfx_password: str) -> dict:
        """Load PFX file and return MSAL-compatible credential dict."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
            pkcs12,
        )

        pfx_data = Path(pfx_path).read_bytes()
        private_key, certificate, _ = pkcs12.load_key_and_certificates(
            pfx_data, pfx_password.encode("utf-8")
        )

        if private_key is None or certificate is None:
            raise GraphAuthError("PFX file does not contain a valid key/certificate pair")

        # MSAL expects PEM-encoded private key and thumbprint
        pem_key = private_key.private_bytes(
            Encoding.PEM,
            format=PrivateFormat.PKCS8,
            encryption_algorithm=NoEncryption(),
        )

        thumbprint = certificate.fingerprint(hashes.SHA1()).hex().upper()

        return {
            "private_key": pem_key.decode("utf-8"),
            "thumbprint": thumbprint,
        }

    def _acquire_token(self) -> str:
        """Acquire an access token via MSAL client credentials flow."""
        result = self._app.acquire_token_for_client(
            scopes=["https://graph.microsoft.com/.default"]
        )

        if "access_token" not in result:
            error = result.get("error_description", result.get("error", "Unknown error"))
            raise GraphAuthError(f"Token acquisition failed: {error}")

        self._token = result["access_token"]
        return self._token

    @property
    def token(self) -> str:
        """Get a valid access token (acquires if needed)."""
        if not self._token:
            self._acquire_token()
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def get(self, endpoint: str, params: dict | None = None) -> dict:
        """Make a GET request to the Graph API.

        Args:
            endpoint: Path relative to GRAPH_BASE (e.g., "/domains") or full URL.
            params: Optional query parameters.

        Returns:
            Parsed JSON response.
        """
        url = endpoint if endpoint.startswith("http") else f"{GRAPH_BASE}{endpoint}"

        resp = self._session.get(url, headers=self._headers(), params=params, timeout=30)

        if resp.status_code == 401:
            # Token may have expired — retry once
            self._acquire_token()
            resp = self._session.get(url, headers=self._headers(), params=params, timeout=30)

        if not resp.ok:
            raise GraphRequestError(
                f"Graph API error {resp.status_code}: {resp.text[:500]}",
                status_code=resp.status_code,
            )

        return resp.json()

    def post(self, endpoint: str, json_body: dict | None = None) -> dict:
        """Make a POST request to the Graph API.

        Args:
            endpoint: Path relative to GRAPH_BASE (e.g., "/users") or full URL.
            json_body: Optional JSON body.

        Returns:
            Parsed JSON response (or empty dict for 204 No Content).
        """
        url = endpoint if endpoint.startswith("http") else f"{GRAPH_BASE}{endpoint}"

        resp = self._session.post(url, headers=self._headers(), json=json_body, timeout=30)

        if resp.status_code == 401:
            self._acquire_token()
            resp = self._session.post(url, headers=self._headers(), json=json_body, timeout=30)

        if not resp.ok:
            raise GraphRequestError(
                f"Graph API error {resp.status_code}: {resp.text[:500]}",
                status_code=resp.status_code,
            )

        if resp.status_code == 204:
            return {}
        return resp.json()

    def get_paginated(
        self,
        endpoint: str,
        params: dict | None = None,
        max_pages: int = MAX_PAGES,
    ) -> list:
        """Make a paginated GET request, following @odata.nextLink.

        Returns all items from the ``value`` array across pages.
        """
        items = []
        url = endpoint if endpoint.startswith("http") else f"{GRAPH_BASE}{endpoint}"
        page = 0

        while url and page < max_pages:
            data = self.get(url, params=params if page == 0 else None)
            items.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
            page += 1

        return items
