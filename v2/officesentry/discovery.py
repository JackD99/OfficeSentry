"""Which Microsoft 365 tenant a domain belongs to, from Microsoft's public sign-in metadata.

No consent and no credentials: it reads the same OpenID discovery document any
sign-in page does. Used to check that a tenant ID handed back by the consent
callback really belongs to the domain an admin added.
"""

from __future__ import annotations

import re

import requests

from . import clouds

DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
TENANT_GUID = re.compile(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/")


def tenant_id_for_domain(domain: str, session: requests.Session | None = None,
                         cloud: str | None = None) -> str | None:
    """The tenant GUID Microsoft publishes for ``domain``, or None when it can't be found out.

    Asks the sign-in host of ``cloud`` and only believes an answer issued by that host.
    """
    if not DOMAIN_RE.match(domain or ""):
        return None
    login = clouds.get(cloud).login
    http = session or requests.Session()
    try:
        resp = http.get(f"{login}/{domain}/v2.0/.well-known/openid-configuration", timeout=15,
                        allow_redirects=False)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("issuer", "token_endpoint"):
        value = data.get(key)
        if isinstance(value, str) and value.startswith(login + "/") and (m := TENANT_GUID.search(value + "/")):
            return m.group(1)
    return None
