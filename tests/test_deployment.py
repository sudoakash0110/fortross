from pathlib import Path

import pytest
import yaml
from dotenv import dotenv_values
from fastapi.testclient import TestClient

from fortross.main import app
from fortross.settings import Settings, get_settings


@pytest.mark.parametrize("configuration", [".env.example", "render.yaml"])
def test_shipped_configuration_supports_credential_free_deployment(
    configuration, monkeypatch, tmp_path
):
    root = Path(__file__).resolve().parents[1]
    if configuration == ".env.example":
        values = dotenv_values(root / configuration)
    else:
        service = yaml.safe_load((root / configuration).read_text())["services"][0]
        assert service["plan"] == "free"
        assert service["buildCommand"] == "pip install ."
        assert "fortross.main:app" in service["startCommand"]
        assert "--workers 1" in service["startCommand"]
        assert service["healthCheckPath"] == "/healthz"
        values = {item["key"]: str(item["value"]) for item in service["envVars"]}
    assert not {
        "API_KEY", "LINKEDIN_LI_AT", "LINKEDIN_JSESSIONID", "LINKEDIN_COOKIE_FILE",
        "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN", "ALLOWED_PROFILE_SLUGS",
    }.intersection(values)
    assert all(key.lower() in Settings.model_fields or key == "PYTHON_VERSION" for key in values)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    # Validate the local template under production requirements too; never use real state.
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SAFETY_STATE_FILE", str(tmp_path / "safety.sqlite3"))
    settings = get_settings()
    settings.validate_server_configuration()
    assert settings.linkedin_profile_access == "any"
    assert settings.linkedin_max_concurrency == 1
    assert settings.linkedin_max_retries == 0
    assert settings.linkedin_max_requests_per_hour == 10
    assert settings.linkedin_max_requests_per_day == 20
    assert not settings.linkedin_li_at and not settings.linkedin_jsessionid
    with TestClient(app) as client:
        assert client.get("/healthz").json()["live_linkedin_enabled"] is False
        assert client.get("/playground").status_code == 200
        login = client.post(
            "/login-with-cookie",
            data={"cookie": 'li_at=synthetic; JSESSIONID="ajax:synthetic"'},
        )
        assert login.status_code == 200
        assert login.json()["linkedin_session_verified"] is False
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}
        result = client.post(
            "/profiles", headers=headers,
            json={"url": "https://www.linkedin.com/in/example/", "include_sections": False},
        )
        assert result.status_code == 503
        assert result.json()["detail"]["code"] == "linkedin_live_disabled"
        assert client.post("/logout", headers=headers).status_code == 200
        assert client.post("/logout", headers=headers).status_code == 401


def test_production_ignores_owner_cookie_file_and_uses_login_cookie(monkeypatch, tmp_path):
    cookie = tmp_path / ".cookie"
    cookie.write_text('li_at=synthetic-session; JSESSIONID="ajax:123"; unrelated=discard')
    state = tmp_path / ".state" / "safety.sqlite3"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LINKEDIN_LIVE_ENABLED", "true")
    monkeypatch.setenv("LINKEDIN_COOKIE_FILE", str(cookie))
    monkeypatch.setenv("SAFETY_STATE_FILE", str(state))
    monkeypatch.setenv("ALLOWED_PROFILE_SLUGS", "example")
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/docs").status_code == 200
        manager = client.app.state.session_manager
        assert manager.settings.linkedin_li_at == ""
        assert manager.settings.linkedin_cookie_file == ""
        assert not manager._sessions
        login = client.post(
            "/login-with-cookie",
            data={"cookie": "li_at=tester-only; JSESSIONID=ajax:tester"},
        )
        assert login.status_code == 200
        assert (
            client.post(
                "/v1/posts", json={"url": "https://www.linkedin.com/in/example/"}
            ).status_code
            == 401
        )
        denied = client.post(
            "/v1/posts",
            headers={"Authorization": "Bearer " + login.json()["access_token"]},
            json={"url": "https://www.linkedin.com/in/not-allowed/", "limit": 5},
        )
        assert denied.status_code == 403
        assert denied.json()["detail"]["code"] == "profile_not_allowed"
        cookies = next(iter(manager._sessions.values())).service.client._client.cookies
        assert set(cookies.keys()) == {"li_at", "JSESSIONID"}
        assert cookies.get("li_at") == "tester-only"
    assert state.exists()


def test_production_can_deploy_disabled_before_session_setup(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SAFETY_STATE_FILE", str(tmp_path / "safety.sqlite3"))
    monkeypatch.setenv("ALLOWED_PROFILE_SLUGS", "example")
    with TestClient(app) as client:
        assert client.get("/healthz").json()["live_linkedin_enabled"] is False
        login = client.post(
            "/login-with-cookie",
            data={"cookie": "li_at=tester-only; JSESSIONID=ajax:tester"},
        )
        result = client.post(
            "/v1/posts",
            headers={"Authorization": "Bearer " + login.json()["access_token"]},
            json={"url": "https://www.linkedin.com/in/example/", "limit": 5},
        )
        assert result.status_code == 503
        assert result.json()["detail"]["code"] == "linkedin_live_disabled"
