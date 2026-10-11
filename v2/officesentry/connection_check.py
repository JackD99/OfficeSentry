"""Check a tenant's app connection and explain sign-in failures in plain words.

Microsoft reports sign-in problems as AADSTS codes. ``explain()`` turns the
common ones into what's wrong and how to fix it, for the "Test connection"
button, the client ID form, failed collection runs and the consent callback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import clouds
from .graph import GraphClient, GraphError
from .msauth import TokenError, certificate_token_provider


@dataclass
class Problem:
    title: str  # what's wrong, one short sentence
    fix: str  # what to do about it
    code: str = ""  # AADSTS code, for support

    def __str__(self) -> str:
        return f"{self.title} {self.fix}"


# AADSTS code -> (what's wrong, how to fix it).
KNOWN = {
    "700027": (
        "The app doesn't have Office Sentry's certificate.",
        "Download the .cer from App connection and upload it to the app registration under "
        "Certificates & secrets > Certificates. A client secret isn't enough. If you've just uploaded it, "
        "wait 5 to 10 minutes: a new certificate takes a while to reach all of Microsoft's servers.",
    ),
    "700016": (
        "This tenant doesn't have the app yet.",
        "Either admin consent hasn't been granted in this tenant (send its Global Administrator the "
        "consent link), or the client ID saved under App connection is wrong.",
    ),
    "50194": (
        "The app only works in the tenant it was created in.",
        "In the app registration, open Authentication and set Supported account types to "
        '"Accounts in any organizational directory" (multi-tenant).',
    ),
    "65001": (
        "Admin consent hasn't been granted in this tenant.",
        "Send the client's Global Administrator the consent link from Data collection.",
    ),
    "7000229": (
        "Admin consent hasn't been granted in this tenant.",
        "Send the client's Global Administrator the consent link from Data collection.",
    ),
    "500113": (
        "The app registration is missing Office Sentry's redirect URI.",
        "In the app registration, open Authentication > Add a platform > Web, and add the redirect URI "
        "shown on the App connection page.",
    ),
    "50011": (
        "The app registration is missing Office Sentry's redirect URI.",
        "In the app registration, open Authentication > Add a platform > Web, and add the redirect URI "
        "shown on the App connection page.",
    ),
    "90002": (
        "Microsoft can't find this tenant.",
        "Check the tenant ID or domain on the client, and its Microsoft cloud: a GCC High, DoD or China "
        "tenant can only be reached through that cloud, with an app registered there.",
    ),
    "900023": ("Microsoft can't find this tenant.", "Check the tenant ID or domain on the client."),
    "53003": (
        "A Conditional Access policy in this tenant blocks the app.",
        "Ask the client's admin to exclude Office Sentry's app from the blocking policy (Conditional Access "
        "for workload identities), or allow the server's location.",
    ),
    "7000112": (
        "The app is disabled in this tenant.",
        "In the tenant's Enterprise applications, open the app's Properties and set "
        '"Enabled for users to sign in" to Yes.',
    ),
    "700024": (
        "The server's clock is wrong, so Microsoft rejects the sign-in.",
        "Set the server's time to sync automatically, then try again.",
    ),
}
AADSTS = re.compile(r"AADSTS(\d+)")


def explain(message: str, code: str = "") -> Problem | None:
    """A plain explanation of a sign-in error, or None when it isn't a known sign-in problem."""
    m = AADSTS.search(f"{code} {message}")
    if not m:
        return None
    known = KNOWN.get(m.group(1))
    if known:
        return Problem(*known, code=f"AADSTS{m.group(1)}")
    first = AADSTS.sub("", message.split("Trace ID")[0]).lstrip(": ").strip().split(". ")[0].rstrip(".")
    return Problem("Microsoft refused the sign-in.", f"Microsoft says: {first}.", f"AADSTS{m.group(1)}")


@dataclass
class CheckResult:
    ok: bool
    title: str
    fix: str = ""
    organisation: str | None = None


def check(tenant_ref: str, client_id: str, key_pem: str, thumbprint: str, cloud: str | None = None) -> CheckResult:
    """Sign in as the app and read the tenant's name, in the tenant's cloud. Read-only, one Graph call."""
    c = clouds.get(cloud)
    provider = certificate_token_provider(tenant_ref, client_id, key_pem, thumbprint, c.graph, c.key)

    def token() -> str:
        try:
            return provider()
        except TokenError as exc:
            raise GraphError(401, exc.code, exc.message) from exc

    try:
        orgs = GraphClient(token, max_retries=1, cloud=c.key).get("/organization", params={"$select": "displayName"})
    except GraphError as exc:
        problem = explain(exc.message, exc.code)
        if problem:
            return CheckResult(False, problem.title, problem.fix)
        if exc.status == 403:
            return CheckResult(
                False,
                "Signed in, but the app can't read the tenant.",
                "Admin consent is missing or incomplete. Send the client's Global Administrator the "
                "consent link again (it covers every permission).",
            )
        return CheckResult(False, "The connection test failed.", exc.message[:300])
    name = ((orgs.get("value") or [{}])[0]).get("displayName")
    return CheckResult(True, f"Connected to {name}." if name else "Connected.", organisation=name)
