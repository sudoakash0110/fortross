import pytest
from pydantic import ValidationError

from fortross.settings import Settings


def test_allowlist_accepts_comma_separated_environment_value(monkeypatch) -> None:
    monkeypatch.setenv("ALLOWED_PROFILE_SLUGS", "first-profile, Second-Profile")
    settings = Settings(_env_file=None)
    assert settings.allowed_profile_slugs == ("first-profile", "second-profile")


def test_invalid_access_mode_fails_closed():
    with pytest.raises(ValidationError):
        Settings(linkedin_profile_access="all")


def test_any_mode_still_requires_credentials():
    with pytest.raises(ValueError, match="LINKEDIN_LI_AT"):
        Settings(linkedin_profile_access="any", api_key="test").validate_live_configuration()


def test_allowlist_mode_requires_targets():
    with pytest.raises(ValueError, match="ALLOWED_PROFILE_SLUGS"):
        Settings(
            api_key="test", linkedin_li_at="fake", linkedin_jsessionid="fake"
        ).validate_live_configuration()
