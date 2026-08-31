import pytest
from fastapi.testclient import TestClient

from fortross.main import app
from fortross.settings import get_settings


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("LINKEDIN_PROFILE_ACCESS", "any")
    monkeypatch.setenv("LOGIN_LIMIT_PER_MINUTE", "60")
    with TestClient(app) as client:
        yield client


def login(api, name="alice"):
    return api.post(
        "/login-with-cookie",
        data={"cookie": f"li_at=secret-{name}; JSESSIONID=ajax:{name}"},
    )


def test_login_logout_isolation_and_api_key_cannot_fetch(api):
    a, b = login(api, "alice"), login(api, "bob")
    assert a.status_code == b.status_code == 200
    assert a.json()["linkedin_session_verified"] is False
    assert a.json()["access_token"] != b.json()["access_token"]
    assert a.headers["cache-control"] == "no-store"
    assert "secret-alice" not in a.text
    payload = {"url": "https://www.linkedin.com/in/example/", "limit": 5}
    assert (
        api.post(
            "/v1/posts", headers={"X-API-Key": "test-invitation-key"}, json=payload
        ).status_code
        == 401
    )
    a_header = {"Authorization": "Bearer " + a.json()["access_token"]}
    b_header = {"Authorization": "Bearer " + b.json()["access_token"]}
    assert api.post("/logout", headers=a_header).status_code == 200
    for route in ("/profiles", "/posts", "/v1/profiles", "/v1/posts"):
        assert api.post(route, headers=a_header, json=payload).status_code == 401
    assert api.post("/v1/posts", headers=a_header, json=payload).status_code == 401
    assert api.post("/logout", headers=a_header).status_code == 401
    assert (
        api.post("/v1/posts", headers=b_header, json=payload).json()["detail"]["code"]
        == "linkedin_live_disabled"
    )
    assert api.post("/logout", headers=b_header).status_code == 200
    fresh = login(api, "alice").json()["access_token"]
    assert fresh != a.json()["access_token"]
    assert api.post("/logout", headers=a_header).status_code == 401
    assert api.post("/logout", headers={"Authorization": "Bearer " + fresh}).status_code == 200


@pytest.mark.parametrize("cookie", ["", "sensitive-value" * 6000])
def test_validation_never_echoes_cookie(api, cookie):
    result = api.post("/login-with-cookie", data={"cookie": cookie})
    assert result.status_code in (413, 422)
    assert "sensitive-value" not in result.text
    assert not api.app.state.session_manager._sessions


def test_malformed_cookie_json_and_oversized_body_do_not_leak(api, caplog):
    result = api.post(
        "/login-with-cookie",
        content='{"cookie":"sensitive-value" garbage',
    )
    assert result.status_code == 422 and "sensitive-value" not in result.text
    result = api.post(
        "/login-with-cookie",
        data={"cookie": "li_at=sensitive-value"},
    )
    assert result.status_code == 400 and "sensitive-value" not in result.text
    result = api.post("/login-with-cookie", content=b"sensitive-value" * 10000)
    assert result.status_code == 413 and "sensitive-value" not in result.text
    assert "sensitive-value" not in caplog.text


def test_public_login_limits_invalid_attempts(monkeypatch):
    monkeypatch.setenv("LOGIN_LIMIT_PER_MINUTE", "1")
    with TestClient(app) as api:
        payload = {"cookie": "missing-required-cookies"}
        assert api.post("/login-with-cookie", data=payload).status_code == 400
        assert api.post("/login-with-cookie", data=payload).status_code == 429
        assert not api.app.state.session_manager._sessions


def test_runtime_ignores_legacy_owner_secrets_and_missing_file(monkeypatch):
    monkeypatch.setenv("LINKEDIN_LI_AT", "must-not-load-owner")
    monkeypatch.setenv("LINKEDIN_JSESSIONID", "ajax:owner")
    monkeypatch.setenv("LINKEDIN_COOKIE_FILE", "/does/not/exist/.cookie")
    settings = get_settings()
    assert (
        settings.linkedin_li_at
        == settings.linkedin_jsessionid
        == settings.linkedin_cookie_file
        == ""
    )
    with TestClient(app) as api:
        assert api.get("/healthz").status_code == 200
        assert not api.app.state.session_manager._sessions


def test_audit_logs_use_session_ids_not_credentials(api, caplog):
    result = login(api)
    token = result.json()["access_token"]
    api.post("/logout", headers={"Authorization": "Bearer " + token})
    assert result.json()["session_id"] in caplog.text
    assert token not in caplog.text and "secret-alice" not in caplog.text


def test_session_tokens_do_not_survive_api_restart(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-invitation-key")
    with TestClient(app) as first:
        token = login(first).json()["access_token"]
    with TestClient(app) as second:
        assert (
            second.post("/logout", headers={"Authorization": "Bearer " + token}).status_code == 401
        )


def test_playground_exposes_public_login_and_token_only_routes(api):
    schema = api.get("/openapi.json").json()
    assert not schema["paths"]["/login-with-cookie"]["post"].get("security")
    content = schema["paths"]["/login-with-cookie"]["post"]["requestBody"]["content"]
    assert set(content) == {"application/x-www-form-urlencoded"}
    name = content["application/x-www-form-urlencoded"]["schema"]["$ref"].split("/")[-1]
    form = schema["components"]["schemas"][name]
    assert form["required"] == ["cookie"]
    assert form["properties"]["cookie"]["format"] == "password"
    assert set(schema["components"]["securitySchemes"]) == {"SessionToken"}
    for route in ("/profiles", "/posts", "/logout"):
        assert schema["paths"][route]["post"]["security"] == [{"SessionToken": []}]
        assert (
            api.post(route, json={"url": "https://www.linkedin.com/in/example/"}).status_code == 401
        )
    assert "/v1/posts" not in schema["paths"]
    assert "/v1/profiles" not in schema["paths"]
    page = api.get("/playground")
    assert page.status_code == 200 and "SwaggerUIBundle" in page.text
    assert '"persistAuthorization": false' in page.text
    assert api.get("/docs", follow_redirects=False).headers["location"] == "/playground"


@pytest.mark.parametrize("multipart", [False, True])
def test_form_preserves_raw_quotes_and_special_characters(api, caplog, multipart):
    raw = (
        'bcookie="v=2&synthetic"; g_state={"x":1}; '
        'li_at=synthetic+percent%2F=value; JSESSIONID="ajax:quoted"'
    )
    kwargs = {"files": {"cookie": (None, raw)}} if multipart else {"data": {"cookie": raw}}
    result = api.post("/login-with-cookie", **kwargs)
    assert result.status_code == 200
    session = next(iter(api.app.state.session_manager._sessions.values()))
    assert session.service.client.settings.linkedin_li_at == "synthetic+percent%2F=value"
    assert session.service.client.settings.linkedin_jsessionid == "ajax:quoted"
    assert raw not in result.text and raw not in caplog.text
    token = result.json()["access_token"]
    headers = {"Authorization": "Bearer " + token}
    assert api.post("/logout", headers=headers).status_code == 200
    assert api.post("/logout", headers=headers).status_code == 401


def test_login_requires_form_instead_of_json_and_never_echoes_input(api):
    result = api.post(
        "/login-with-cookie",
        json={"cookie": "li_at=sensitive-value; JSESSIONID=ajax:test"},
    )
    assert result.status_code == 422
    assert "sensitive-value" not in result.text
    assert not api.app.state.session_manager._sessions


@pytest.mark.parametrize("route", ["/profiles", "/posts"])
def test_new_routes_accept_cookie_session_token(api, route):
    token = login(api).json()["access_token"]
    result = api.post(
        route,
        headers={"Authorization": "Bearer " + token},
        json={"url": "https://www.linkedin.com/in/example/"},
    )
    assert result.status_code == 503
    assert result.json()["detail"]["code"] == "linkedin_live_disabled"


def test_audit_id_and_forged_tokens_cannot_revoke_a_session(api):
    result = login(api).json()
    for forged in (result["session_id"], result["access_token"] + "changed", "test-invitation-key"):
        assert api.post("/logout", headers={"Authorization": "Bearer " + forged}).status_code == 401
    assert (
        api.post(
            "/logout", headers={"Authorization": "Bearer " + result["access_token"]}
        ).status_code
        == 200
    )


async def test_api_logout_interrupts_active_request_without_unhandled_cancellation(monkeypatch):
    import asyncio

    import httpx

    from fortross.main import lifespan

    monkeypatch.setenv("API_KEY", "invite")
    monkeypatch.setenv("LINKEDIN_LIVE_ENABLED", "true")
    monkeypatch.setenv("LINKEDIN_PROFILE_ACCESS", "any")
    async with lifespan(app):
        manager = app.state.session_manager
        token, session = await manager.create("li_at=alice; JSESSIONID=ajax:alice")
        entered = asyncio.Event()

        async def handler(request):
            entered.set()
            await asyncio.Event().wait()

        await session.service.client._client.aclose()
        session.service.client._client = httpx.AsyncClient(
            base_url="https://www.linkedin.com", transport=httpx.MockTransport(handler)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Authorization": "Bearer " + token}
            request = asyncio.create_task(
                client.post(
                    "/v1/posts",
                    headers=headers,
                    json={"url": "https://www.linkedin.com/in/example/"},
                )
            )
            await asyncio.wait_for(entered.wait(), 2)
            logout = await client.post("/logout", headers=headers)
            result = await asyncio.wait_for(request, 2)
            assert logout.status_code == 200
            assert result.status_code == 401
