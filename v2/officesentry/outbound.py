"""Outbound HTTPS to addresses an admin or analyst typed: webhooks and MTA-STS policy files.

``public_address`` resolves the host once and refuses it if any address is
internal (private, loopback, link-local, reserved...). ``pinned_session``
then connects to exactly that address, keeping the host name for TLS (SNI and
the certificate check) and the Host header, so a DNS answer that changes
between the check and the request (DNS rebinding) can't send the request to an
internal address.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter


class LookupFailed(Exception):
    pass


class NotPublic(Exception):
    def __init__(self, host: str, address: str):
        super().__init__(f"{host} resolves to an internal address ({address})")
        self.host, self.address = host, address


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (
        not (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        )
        and ip.is_global
    )


def resolve(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError) as exc:
        raise LookupFailed(host) from exc
    addresses = [info[4][0].split("%", 1)[0] for info in infos]
    if not addresses:
        raise LookupFailed(host)
    return addresses


def public_address(host: str, port: int = 443) -> str:
    """The address to connect to, if every address ``host`` resolves to is public. Raises otherwise."""
    addresses = resolve(host, port)
    for address in addresses:
        if not _is_public(ipaddress.ip_address(address)):
            raise NotPublic(host, address)
    return addresses[0]


class PinnedAdapter(HTTPAdapter):
    """Connects to one vetted address whatever DNS says later; TLS still checks the certificate for the name."""

    def __init__(self, address: str, **kwargs):
        self.address = address
        super().__init__(**kwargs)

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        name = host_params["host"]
        host_params = {**host_params, "host": self.address}
        if host_params["scheme"] == "https":
            pool_kwargs = {**pool_kwargs, "server_hostname": name, "assert_hostname": name}
        return host_params, pool_kwargs


def pinned_session(url: str, address: str | None) -> tuple[requests.Session, dict]:
    """A session that sends ``url`` to ``address`` (None: resolve normally), and the Host header to add."""
    http = requests.Session()
    if address is None:
        return http, {}
    http.mount("https://", PinnedAdapter(address))
    http.mount("http://", PinnedAdapter(address))
    return http, {"Host": urlsplit(url).netloc.rsplit("@", 1)[-1]}
