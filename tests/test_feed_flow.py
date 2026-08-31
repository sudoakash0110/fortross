import httpx
import pytest
from fastapi import FastAPI
from test_feed import feed_payload
from test_voyager import TARGET, bootstrap

from fortross.api import get_rate_limiter, get_service, router
from fortross.linkedin.client import LinkedInClient
from fortross.safety import MemoryCounterStore, RateLimiter
from fortross.service import ProfileService
from fortross.sessions import SessionManager
from fortross.settings import Settings, get_settings


@pytest.mark.parametrize("feed_status,expected", [(200, 200), (403, 401), (429, 503), (503, 502)])
async def test_posts_route_bootstrap_and_one_feed_get(feed_status, expected):
    settings = Settings(
        api_key="test-key",
        linkedin_live_enabled=True,
        linkedin_li_at="fake-session",
        linkedin_jsessionid="ajax:fake",
        allowed_profile_slugs=("example",),
        linkedin_max_retries=1,
    )
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.host == "www.linkedin.com"
        assert request.headers["csrf-token"] == "ajax:fake"
        if len(calls) == 1:
            assert request.url.path == "/in/example/recent-activity/shares/"
            return httpx.Response(200, text=bootstrap(TARGET))
        assert len(calls) == 2
        assert request.url.path == "/voyager/api/graphql"
        assert "count:2,start:0" in request.url.params["variables"]
        assert "paginationToken" not in str(request.url)
        return httpx.Response(feed_status, json=feed_payload())

    upstream = LinkedInClient(settings)
    upstream.governor._interval = 0
    headers, cookies = upstream._client.headers, upstream._client.cookies
    await upstream._client.aclose()
    upstream._client = httpx.AsyncClient(
        base_url="https://www.linkedin.com",
        transport=httpx.MockTransport(handler),
        headers=headers,
        cookies=cookies,
    )
    app = FastAPI()
    app.include_router(router)
    manager = SessionManager(settings, MemoryCounterStore())
    app.state.session_manager = manager
    token, session = await manager.create("li_at=fake-session; JSESSIONID=ajax:fake")
    await session.service.client.close()
    session.service = ProfileService(settings, upstream)
    app.dependency_overrides[get_settings] = lambda: settings
    service = ProfileService(settings, upstream)
    limiter = RateLimiter(settings, MemoryCounterStore())
    app.dependency_overrides[get_service] = lambda: service
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            result = await client.post(
                "/v1/posts",
                headers={"Authorization": "Bearer " + token},
                json={"url": "https://www.linkedin.com/in/example/", "limit": 2},
            )
        assert result.status_code == expected
        assert len(calls) == 2
        if expected == 200:
            body = result.json()
            assert [post["id"] for post in body["posts"]] == ["11111", "22222"]
            assert body["posts"][1]["original_post"]["id"] == "33333"
            assert body["pagination_followed"] is False
    finally:
        await manager.close()


async def test_bootstrap_budget_blocks_feed_before_network():
    from fortross.linkedin.errors import LinkedInSafetyError
    from fortross.linkedin.urls import parse_profile_url

    settings = Settings(
        api_key="test-key",
        linkedin_live_enabled=True,
        linkedin_li_at="fake",
        linkedin_jsessionid="ajax:fake",
        allowed_profile_slugs=("example",),
        linkedin_max_requests_per_hour=1,
    )
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=bootstrap(TARGET))

    upstream = LinkedInClient(settings)
    upstream.governor._interval = 0
    await upstream._client.aclose()
    upstream._client = httpx.AsyncClient(
        base_url="https://www.linkedin.com", transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(LinkedInSafetyError):
            await ProfileService(settings, upstream).fetch_posts(
                parse_profile_url("https://www.linkedin.com/in/example/"), 5
            )
        assert len(calls) == 1
    finally:
        await upstream.close()


async def test_post_text_cannot_trigger_html_login_detector():
    upstream = LinkedInClient(Settings())
    await upstream._client.aclose()
    upstream._client = httpx.AsyncClient(
        base_url="https://www.linkedin.com",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"text": "Sign in to LinkedIn; authwall; /checkpoint/"}
            )
        ),
    )
    try:
        assert "authwall" in await upstream._get_once("/voyager/api/graphql")
    finally:
        await upstream.close()
