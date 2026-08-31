import pytest

from fortross.linkedin.errors import LinkedInDisabledError, ProfileNotAllowedError
from fortross.linkedin.urls import parse_profile_url
from fortross.service import ProfileService
from fortross.settings import Settings


class StubClient:
    async def fetch_profile_documents(self, slug: str):
        raise AssertionError("disabled or denied requests must not hit LinkedIn")

    async def fetch_posts_document(self, slug: str):
        raise AssertionError("disabled or denied requests must not hit LinkedIn")


async def test_live_mode_defaults_off() -> None:
    service = ProfileService(Settings(), client=StubClient())
    with pytest.raises(LinkedInDisabledError):
        await service.fetch(parse_profile_url("https://www.linkedin.com/in/example/"))


async def test_allowlist_is_enforced_before_upstream() -> None:
    settings = Settings(
        api_key="test-key",
        linkedin_live_enabled=True,
        linkedin_li_at="session",
        linkedin_jsessionid="ajax:test",
        allowed_profile_slugs=("allowed",),
    )
    service = ProfileService(settings, client=StubClient())
    with pytest.raises(ProfileNotAllowedError):
        await service.fetch(parse_profile_url("https://www.linkedin.com/in/not-allowed/"))


class RecordingClient:
    def __init__(self):
        self.calls = []

    async def fetch_profile_documents(self, slug, include_sections=True):
        self.calls.append((slug, include_sections))
        return {"profile": "<h1>Example Person</h1>"}, []

    async def fetch_posts_document(self, slug):
        self.calls.append(slug)
        return "<main>No posts yet</main>"


async def test_any_mode_accepts_profile_without_allowlist():
    settings = Settings(
        api_key="test",
        linkedin_live_enabled=True,
        linkedin_profile_access="any",
        linkedin_li_at="fake",
        linkedin_jsessionid="fake",
    )
    client = RecordingClient()
    service = ProfileService(settings, client)
    target = parse_profile_url("https://www.linkedin.com/in/another-person/")
    await service.fetch(target, include_sections=False)
    posts, _, _ = await service.fetch_posts(target)
    assert posts == []
    assert client.calls == [("another-person", False), "another-person"]


async def test_posts_obey_allowlist():
    settings = Settings(
        api_key="test",
        linkedin_live_enabled=True,
        allowed_profile_slugs=("allowed",),
        linkedin_li_at="fake",
        linkedin_jsessionid="fake",
    )
    service = ProfileService(settings, StubClient())
    with pytest.raises(ProfileNotAllowedError):
        await service.fetch_posts(parse_profile_url("https://www.linkedin.com/in/denied/"))


async def test_posts_disabled_before_upstream():
    service = ProfileService(Settings(), StubClient())
    with pytest.raises(LinkedInDisabledError):
        await service.fetch_posts(parse_profile_url("https://www.linkedin.com/in/example/"))


async def test_missing_profile_identity_fails_instead_of_returning_empty_success():
    from fortross.linkedin.errors import ParseError

    class ShellClient:
        async def fetch_profile_documents(self, slug, include_sections):
            return {"profile": "<main>App shell</main>"}, []

    settings = Settings(
        api_key="test",
        linkedin_live_enabled=True,
        linkedin_profile_access="any",
        linkedin_li_at="fake",
        linkedin_jsessionid="fake",
    )
    service = ProfileService(settings, ShellClient())
    with pytest.raises(ParseError, match="profile identity"):
        await service.fetch(parse_profile_url("https://www.linkedin.com/in/example/"))


async def test_uncertain_base_fields_have_warnings():
    settings = Settings(
        api_key="test",
        linkedin_live_enabled=True,
        linkedin_profile_access="any",
        linkedin_li_at="fake",
        linkedin_jsessionid="fake",
    )
    service = ProfileService(settings, RecordingClient())
    profile, warnings = await service.fetch(
        parse_profile_url("https://www.linkedin.com/in/example/"), False
    )
    assert profile.headline is None
    assert any("headline" in warning for warning in warnings)
    assert any("location" in warning for warning in warnings)
