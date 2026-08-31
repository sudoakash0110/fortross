import pytest
from fastapi.testclient import TestClient

from fortross.api import get_service
from fortross.main import app
from fortross.models import LinkedInPost, LinkedInProfile


def test_health_is_public_and_reports_live_state() -> None:
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "live_linkedin_enabled": False}


def test_profile_endpoint_requires_api_key() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/v1/profiles",
            json={"url": "https://www.linkedin.com/in/example/"},
        )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_api_key"


@pytest.fixture
def authenticated_api(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-api-key")
    calls = []

    class Service:
        async def fetch_posts(self, target, limit):
            calls.append((target.slug, limit))
            return (
                [
                    LinkedInPost(
                        id="12345",
                        url="https://www.linkedin.com/feed/update/urn:li:activity:12345/",
                    )
                ],
                False,
                [],
            )

        async def fetch(self, target, include_sections):
            calls.append((target.slug, include_sections))
            return LinkedInProfile(
                source_url=target.canonical_url, public_identifier=target.slug
            ), []

    app.dependency_overrides[get_service] = lambda: Service()
    try:
        with TestClient(app) as client:
            yield client, calls
    finally:
        app.dependency_overrides.clear()


def test_posts_api_contract(authenticated_api):
    client, calls = authenticated_api
    result = client.post(
        "/v1/posts",
        headers={"X-API-Key": "test-api-key"},
        json={
            "url": "https://www.linkedin.com/in/example/",
            "limit": 5,
        },
    )
    assert result.status_code == 200
    assert result.json()["scope"] == "first_page"
    assert result.json()["pagination_followed"] is False
    assert result.json()["has_more"] is None
    assert len(result.json()["posts"]) == 1
    assert calls == [("example", 5)]


@pytest.mark.parametrize("limit", [0, 51])
def test_posts_rejects_invalid_limit(authenticated_api, limit):
    client, calls = authenticated_api
    result = client.post(
        "/v1/posts",
        headers={"X-API-Key": "test-api-key"},
        json={
            "url": "https://www.linkedin.com/in/example/",
            "limit": limit,
        },
    )
    assert result.status_code == 422
    assert calls == []


def test_posts_rejects_other_websites(authenticated_api):
    client, calls = authenticated_api
    result = client.post(
        "/v1/posts",
        headers={"X-API-Key": "test-api-key"},
        json={
            "url": "https://example.com/in/example/",
        },
    )
    assert result.status_code == 400
    assert calls == []


def test_posts_requires_key(authenticated_api):
    client, calls = authenticated_api
    result = client.post("/v1/posts", json={"url": "https://www.linkedin.com/in/example/"})
    assert result.status_code == 401
    assert calls == []


def test_profile_base_only_option(authenticated_api):
    client, calls = authenticated_api
    result = client.post(
        "/v1/profiles",
        headers={"X-API-Key": "test-api-key"},
        json={
            "url": "https://www.linkedin.com/in/example/",
            "include_sections": False,
        },
    )
    assert result.status_code == 200
    assert calls == [("example", False)]
