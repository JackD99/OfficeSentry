"""Calls to Microsoft stay on supported ground: beta only with a reason, and no retired fields asked for."""

from urllib.parse import parse_qsl, urlsplit

from officesentry import endpoints, events
from officesentry.collectors import sharepoint
from tests import graphdata as gd
from tests import replay
from tests import sharepoint_data as spd


class _Reads(dict):
    """A dict that remembers which keys were read."""

    def __init__(self, *args):
        super().__init__(*args)
        self.read: set[str] = set()

    def get(self, key, default=None):
        self.read.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.read.add(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.read.add(key)
        return super().__contains__(key)


def test_every_beta_call_says_why():
    """A beta Graph call needs a reason (what v1.0 lacks). Calls not listed as beta fail the run telemetry
    tests in test_ratelimit.py, so this covers every beta path the collectors use."""
    unexplained = [e.path for e in endpoints.GRAPH if "beta" in e.versions and not e.beta.strip()]
    assert unexplained == []
    assert [e.path for e in endpoints.GRAPH if "beta" not in e.versions and e.beta] == []


def test_recorded_beta_calls_are_listed_as_beta():
    beta = {
        endpoints.graph_key(e["url"])
        for e in replay.load()["entries"]
        if e["api"] == "graph" and endpoints.split(e["url"])[0] == "beta"
    }
    assert beta, "the recording has beta calls"
    for key in beta:
        version, _, path = key.partition(" ")
        entry = next(e for e in endpoints.GRAPH if e.path == path.split(" (")[0] and version in e.versions)
        assert entry.beta, key


def test_sign_in_fields_cover_what_is_stored():
    """The sign-in log asks for SIGNIN_FIELDS only; every field the stored row reads must be among them."""
    read: set[str] = set()
    for s in gd.SIGNINS:
        spy = _Reads(s)
        events._signin_row(lambda v: v, spy)
        read |= spy.read
    assert read <= set(events.SIGNIN_FIELDS.split(",")) - {"id", "createdDateTime"} | {"riskEventTypes"}
    assert "riskEventTypes" not in events.SIGNIN_FIELDS.split(",")  # a fallback only; beta no longer has it


def test_permission_fields_cover_what_is_read():
    """Drive item permissions ask for PERMISSION_FIELDS only (never the retired grantedTo and
    grantedToIdentities); every field describe_permissions reads must be among them."""
    fields = set(sharepoint.PERMISSION_FIELDS.split(","))
    assert not fields & {"grantedTo", "grantedToIdentities"}
    read: set[str] = set()
    for perms in spd.PERMISSIONS.values():
        spies = [_Reads(p) for p in perms]
        sharepoint.describe_permissions(spies, {"x.com"})
        read |= {k for spy in spies for k in spy.read}
    assert read and read <= fields


def test_recorded_calls_ask_only_for_supported_fields():
    """Asking these two calls for every field brings back fields Microsoft retired, with Deprecation and Sunset
    headers. The recording shows what the collectors really send."""
    narrowed = {
        ("beta", "/auditLogs/signIns"): events.SIGNIN_FIELDS,
        ("v1.0", "/drives/{drive}/items/{item}/permissions"): sharepoint.PERMISSION_FIELDS,
    }
    seen = set()
    for e in replay.load()["entries"]:
        if e["api"] != "graph" or (entry := endpoints.graph(e["url"])) is None:
            continue
        version = endpoints.split(e["url"])[0]
        if (version, entry.path) in narrowed:
            seen.add((version, entry.path))
            assert dict(parse_qsl(urlsplit(e["url"]).query)).get("$select") == narrowed[version, entry.path]
    assert seen == set(narrowed)
