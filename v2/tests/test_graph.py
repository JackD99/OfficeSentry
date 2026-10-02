import pytest
import responses

from officesentry.graph import GRAPH_ROOT, GraphClient, GraphError, LicenseRequired, PermissionMissing


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def graph(sleeps):
    return GraphClient(lambda: "token", sleep=sleeps.append)


def test_paths_are_relative_to_the_version():
    assert GraphClient.build_url("/users") == f"{GRAPH_ROOT}/v1.0/users"
    assert GraphClient.build_url("/users", version="beta") == f"{GRAPH_ROOT}/beta/users"


@pytest.mark.parametrize("bad", ["/v1.0/users", "/beta/users", "users"])
def test_version_prefixed_or_relative_paths_are_rejected(bad):
    # v1 shipped /v1.0/v1.0/... URLs in six collectors; this makes that impossible.
    with pytest.raises(ValueError):
        GraphClient.build_url(bad)


def test_token_is_never_sent_to_another_host():
    with pytest.raises(ValueError):
        GraphClient.build_url("https://evil.example.com/v1.0/users")
    with pytest.raises(ValueError):
        GraphClient.build_url("https://graph.microsoft.com.evil.example/v1.0/users")


@responses.activate
def test_get_all_follows_next_links(graph):
    responses.get(f"{GRAPH_ROOT}/v1.0/users", json={
        "value": [{"id": "1"}, {"id": "2"}],
        "@odata.nextLink": f"{GRAPH_ROOT}/v1.0/users?$skiptoken=abc",
    })
    responses.get(f"{GRAPH_ROOT}/v1.0/users?$skiptoken=abc", json={"value": [{"id": "3"}]})
    result = graph.get_all("/users", params={"$top": "2"})
    assert [u["id"] for u in result.items] == ["1", "2", "3"]
    assert result.truncated is False and result.pages == 2
    assert responses.calls[0].request.headers["Authorization"] == "Bearer token"
    # Query params go on the first request only; the next link carries its own.
    assert "%24top=2" in responses.calls[0].request.url
    assert "%24top" not in responses.calls[1].request.url


@responses.activate
def test_paging_limit_is_reported_not_silent(graph):
    responses.get(f"{GRAPH_ROOT}/v1.0/users", json={
        "value": [{"id": "1"}], "@odata.nextLink": f"{GRAPH_ROOT}/v1.0/users?page=2"})
    result = graph.get_all("/users", max_pages=1)
    assert result.truncated is True
    assert len(result.items) == 1


@responses.activate
def test_throttling_honours_retry_after(graph, sleeps):
    url = f"{GRAPH_ROOT}/v1.0/organization"
    responses.get(url, status=429, headers={"Retry-After": "7"})
    responses.get(url, status=503)
    responses.get(url, json={"value": []})
    assert graph.get("/organization") == {"value": []}
    assert sleeps == [7.0, 2.0]


@responses.activate
def test_gives_up_after_max_retries(sleeps):
    graph = GraphClient(lambda: "t", sleep=sleeps.append, max_retries=2)
    responses.get(f"{GRAPH_ROOT}/v1.0/organization", status=429)
    with pytest.raises(GraphError) as exc:
        graph.get("/organization")
    assert exc.value.status == 429
    assert len(sleeps) == 2


@responses.activate
def test_licence_errors_are_typed(graph):
    responses.get(f"{GRAPH_ROOT}/v1.0/users", status=403, json={"error": {
        "code": "Authentication_RequestFromNonPremiumTenantOrB2CTenant",
        "message": "Neither tenant is B2C or tenant doesn't have premium license",
        "innerError": {"request-id": "rid"}}})
    with pytest.raises(LicenseRequired) as exc:
        graph.get("/users")
    assert exc.value.request_id == "rid"


@responses.activate
def test_permission_errors_are_typed(graph):
    responses.get(f"{GRAPH_ROOT}/v1.0/users", status=403, json={"error": {
        "code": "Authorization_RequestDenied", "message": "Insufficient privileges"}})
    with pytest.raises(PermissionMissing):
        graph.get("/users")


def test_client_has_no_write_methods(graph):
    for name in ("post", "patch", "put", "delete"):
        assert not hasattr(graph, name)
