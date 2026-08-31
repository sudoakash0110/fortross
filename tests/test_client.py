import httpx
import pytest

from fortross.linkedin.client import LinkedInClient, UpstreamGovernor
from fortross.linkedin.errors import (
    CircuitOpenError,
    LinkedInAuthError,
    LinkedInChallengeError,
    LinkedInSafetyError,
    LinkedInUpstreamError,
)
from fortross.settings import Settings


def _settings(**overrides) -> Settings:
    values = {
        "linkedin_li_at": "private-session",
        "linkedin_jsessionid": '"ajax:123"',
        "linkedin_min_request_interval_seconds": 0.5,
    }
    values.update(overrides)
    return Settings(**values)


async def test_client_sends_session_headers_without_exposing_them() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["csrf-token"] == "ajax:123"
        assert "li_at=private-session" in request.headers["cookie"]
        return httpx.Response(200, text="<html>profile</html>")

    client = LinkedInClient(_settings())
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://www.linkedin.com",
        transport=httpx.MockTransport(handler),
        headers=client._client.headers,
        cookies=client._client.cookies,
    )
    try:
        assert await client._get("/in/example/") == "<html>profile</html>"
    finally:
        await client.close()


async def test_auth_failure_trips_circuit_breaker() -> None:
    client = LinkedInClient(_settings())
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://www.linkedin.com",
        transport=httpx.MockTransport(lambda _: httpx.Response(403)),
    )
    try:
        with pytest.raises(LinkedInAuthError):
            await client._get("/in/example/")
        with pytest.raises(CircuitOpenError):
            await client._get("/in/example/")
    finally:
        await client.close()


async def test_login_page_trips_circuit_breaker() -> None:
    client = LinkedInClient(_settings())
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://www.linkedin.com",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text='<a href="/checkpoint/">Continue</a>')
        ),
    )
    try:
        with pytest.raises(LinkedInChallengeError):
            await client._get("/in/example/")
    finally:
        await client.close()


async def test_response_size_is_limited_before_download() -> None:
    client = LinkedInClient(_settings(linkedin_max_response_bytes=100_000))
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://www.linkedin.com",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-length": "100001"})
        ),
    )
    try:
        with pytest.raises(LinkedInUpstreamError, match="size limit"):
            await client._get("/in/example/")
    finally:
        await client.close()


async def test_posts_make_one_attempt_even_when_retries_enabled():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(503)

    client = LinkedInClient(_settings(linkedin_max_retries=1))
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://www.linkedin.com",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(LinkedInUpstreamError):
            await client.fetch_posts_document("example")
        assert calls == ["/in/example/recent-activity/shares/"]
    finally:
        await client.close()


async def test_base_only_fetch_makes_one_request():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, text="<h1>Example Person</h1>")

    client = LinkedInClient(_settings())
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://www.linkedin.com",
        transport=httpx.MockTransport(handler),
    )
    try:
        documents, _ = await client.fetch_profile_documents("example", include_sections=False)
        assert list(documents) == ["profile"]
        assert calls == ["/in/example/"]
    finally:
        await client.close()


async def test_upstream_budget_blocks_next_attempt():
    governor = UpstreamGovernor(_settings(linkedin_max_requests_per_hour=1))
    governor._interval = 0
    async with governor.slot():
        pass
    with pytest.raises(LinkedInSafetyError):
        async with governor.slot():
            pytest.fail("Budget must block access")


async def test_queued_request_rechecks_circuit():
    governor = UpstreamGovernor(_settings())
    governor._interval = 0
    import asyncio

    async def queued():
        async with governor.slot():
            pytest.fail("Queued request must not pass an open circuit")

    async with governor.slot():
        task = asyncio.create_task(queued())
        await asyncio.sleep(0)
        await governor.trip()
    with pytest.raises(CircuitOpenError):
        await task


async def test_diagnostic_feed_uses_one_json_get_and_no_retry():
    from test_voyager import TARGET, bootstrap

    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path == "/voyager/api/graphql"
        assert request.headers["accept"] == "application/vnd.linkedin.normalized+json+2.1"
        assert "start:0" in request.url.params["variables"]
        assert "count:5" in request.url.params["variables"]
        return httpx.Response(503)

    client = LinkedInClient(_settings(linkedin_max_retries=1))
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://www.linkedin.com", transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(LinkedInUpstreamError):
            await client.fetch_posts_feed_from_bootstrap(bootstrap(TARGET), "example", 5)
        assert len(calls) == 1
    finally:
        await client.close()
