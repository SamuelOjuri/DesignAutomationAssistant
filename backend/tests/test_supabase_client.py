import httpx
import pytest
from supabase import ClientOptions
from yarl import URL

from backend.app.supabase_client import create_supabase_client


@pytest.mark.parametrize("base_url, prefix", [
    ("https://project.supabase.co", ""),
    ("https://project.supabase.co/", ""),
    ("https://proxy.invalid/supabase/", "/supabase"),
])
def test_storage_endpoint_is_normalized_before_initialization_and_requests_work(base_url, prefix, capsys):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["apikey"] == "test-service-key"
        assert request.headers["authorization"] == "Bearer test-service-key"
        if request.url.path == f"{prefix}/storage/v1/bucket":
            return httpx.Response(200, json=[])
        if request.url.path == f"{prefix}/storage/v1/object/design-processing-artifacts/probe.txt":
            return httpx.Response(200, content=b"stored artifact")
        if request.url.path == f"{prefix}/rest/v1/diagnostic":
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        client = create_supabase_client(base_url, "test-service-key", options=ClientOptions(
            httpx_client=http_client, auto_refresh_token=False, persist_session=False,
        ))
        assert isinstance(client.storage_url, URL)
        assert client.storage_url.path == f"{prefix}/storage/v1/"
        assert client._storage is None
        assert client.storage.list_buckets() == []
        assert client.storage.from_("design-processing-artifacts").download("probe.txt") == b"stored artifact"
        assert client.table("diagnostic").select("id").execute().data == []
    assert len(requests) == 3
    assert "Storage endpoint URL should have a trailing slash" not in capsys.readouterr().out
