import pytest
from pydantic import ValidationError

from fortross.cookies import parse_cookie_header, read_cookie_file
from fortross.settings import Settings


@pytest.mark.parametrize(
    "header",
    [
        'li_at=synthetic-session; JSESSIONID="ajax:123"',
        'Cookie: unrelated={"x":1}; JSESSIONID=ajax:123; li_at=synthetic-session; other=true',
        '\nli_at=synthetic-session; JSESSIONID="ajax:123"\n',
    ],
)
def test_extracts_only_required_cookies(header):
    assert parse_cookie_header(header) == ("synthetic-session", "ajax:123")


@pytest.mark.parametrize(
    "header",
    [
        "li_at=missing-other",
        "JSESSIONID=ajax:missing-other",
        "li_at=; JSESSIONID=ajax:123",
        'li_at="unclosed; JSESSIONID=ajax:123',
        "li_at=first; li_at=second; JSESSIONID=ajax:123",
        "li_at=secret\nInjected: header; JSESSIONID=ajax:123",
    ],
)
def test_rejects_bad_input_without_echoing_it(header):
    with pytest.raises(ValueError) as exc:
        parse_cookie_header(header)
    assert header not in str(exc.value)


def test_explicit_file_overrides_environment_and_is_not_serialized(tmp_path):
    cookie = tmp_path / ".cookie"
    cookie.write_text('li_at=file-secret; JSESSIONID="ajax:file"')
    settings = Settings(
        linkedin_cookie_file=str(cookie),
        linkedin_li_at="stale-value",
        linkedin_jsessionid="ajax:stale",
        api_key="private-api-key",
    )
    assert settings.linkedin_li_at == "file-secret"
    assert settings.linkedin_jsessionid == "ajax:file"
    assert "file-secret" not in repr(settings)
    assert "file-secret" not in settings.model_dump_json()
    assert "private-api-key" not in repr(settings)


def test_missing_file_never_falls_back_to_env(tmp_path):
    with pytest.raises(ValidationError) as exc:
        Settings(
            linkedin_cookie_file=str(tmp_path / "absent"), linkedin_li_at="sensitive-old-value"
        )
    assert "sensitive-old-value" not in str(exc.value)


def test_size_limit_and_encoding(tmp_path):
    cookie = tmp_path / ".cookie"
    cookie.write_bytes(b"x" * 65_537)
    with pytest.raises(ValueError, match="size limit"):
        read_cookie_file(str(cookie))
    cookie.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        read_cookie_file(str(cookie))


def test_production_requires_persistent_state_and_safe_limits():
    values = dict(
        app_env="production",
        api_key="synthetic-long-api-key-32-characters",
        linkedin_li_at="synthetic",
        linkedin_jsessionid="ajax:synthetic",
        allowed_profile_slugs=("example",),
    )
    with pytest.raises(ValueError, match="Turso"):
        Settings(**values).validate_live_configuration()
    values.update(turso_database_url="libsql://example.turso.io", turso_auth_token="fake")
    Settings(**values).validate_live_configuration()
    with pytest.raises(ValueError, match="concurrency"):
        Settings(**values, linkedin_max_concurrency=2).validate_live_configuration()
    # The old API_KEY setting is ignored, including in production.
    Settings(**{**values, "api_key": "short"}).validate_live_configuration()


def test_partial_database_config_is_not_silently_ignored():
    with pytest.raises(ValidationError, match="both TURSO"):
        Settings(turso_database_url="libsql://example.turso.io")


def test_config_check_is_offline_and_never_prints_secrets(monkeypatch, capsys):
    from fortross.check import main

    monkeypatch.setattr("sys.argv", ["fortross.check"])
    monkeypatch.setenv("API_KEY", "private-api-secret")
    monkeypatch.setenv("LINKEDIN_LI_AT", "private-session-secret")
    monkeypatch.setenv("LINKEDIN_JSESSIONID", "ajax:private")
    monkeypatch.setenv("ALLOWED_PROFILE_SLUGS", "example")
    assert main() == 0
    output = capsys.readouterr().out
    assert '"network_requests_made": 0' in output
    assert "private" not in output
