import stat

import pytest

from fortross.diagnostics import capture_client, main
from fortross.linkedin.client import LinkedInClient
from fortross.linkedin.errors import LinkedInSafetyError, LinkedInUpstreamError
from fortross.settings import Settings


async def test_capture_is_bounded_private_and_redacts_credentials(monkeypatch, tmp_path):
    calls = []

    async def response(self, path, *, accept=None):
        calls.append(path)
        return "synthetic-cookie ajax:synthetic ajax%3Asynthetic <h1>Example</h1>"

    monkeypatch.setattr(LinkedInClient, "_get_once", response)
    factory = capture_client("example", tmp_path)
    settings = Settings(linkedin_li_at="synthetic-cookie", linkedin_jsessionid="ajax:synthetic")
    first, second = factory(settings), factory(settings.model_copy())
    try:
        with pytest.raises(LinkedInSafetyError):
            await first.fetch_profile_documents("example", True)
        with pytest.raises(LinkedInSafetyError):
            await first.fetch_posts_document("different")
        with pytest.raises(LinkedInSafetyError):
            await first.fetch_posts_feed_from_bootstrap("", "example", 6)
        assert not calls
        await first.fetch_profile_documents("example", False)
        with pytest.raises(LinkedInSafetyError):
            await second.fetch_profile_documents("example", False)
        await second.fetch_posts_document("example")
        await second._get_once(
            "/voyager/api/graphql?variables=(count:5,start:0,profileUrn:synthetic)"
        )
        with pytest.raises(LinkedInSafetyError):
            await first.fetch_posts_document("example")
        assert len(calls) == 3
        assert {p.name for p in tmp_path.iterdir()} == {
            "profile.html",
            "posts-bootstrap.html",
            "posts-feed.json",
        }
        for path in tmp_path.iterdir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert "synthetic" not in path.read_text()
            assert "<h1>Example</h1>" in path.read_text()
    finally:
        await first.close()
        await second.close()


async def test_failed_capture_stops_further_requests(monkeypatch, tmp_path):
    calls = []

    async def fail(self, path, *, accept=None):
        calls.append(path)
        raise LinkedInUpstreamError("Failed")

    monkeypatch.setattr(LinkedInClient, "_get_once", fail)
    client = capture_client("example", tmp_path)(Settings())
    try:
        with pytest.raises(LinkedInUpstreamError):
            await client.fetch_profile_documents("example", False)
        with pytest.raises(LinkedInSafetyError):
            await client.fetch_posts_document("example")
        assert len(calls) == 1
        assert not list(tmp_path.iterdir())
    finally:
        await client.close()


def test_capture_launcher_refuses_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(
        "sys.argv", ["diagnostics", "--profile-url", "https://www.linkedin.com/in/example/"]
    )
    with pytest.raises(SystemExit):
        main()


async def test_one_opt_in_section_reuses_profile_without_refetching(monkeypatch, tmp_path):
    calls = []

    async def response(self, path, *, accept=None):
        calls.append(path)
        return "<section><h2>Experience</h2></section>"

    monkeypatch.setattr(LinkedInClient, "_get_once", response)
    saved = "<h1>Example Person</h1>"
    client = capture_client("example", tmp_path, sections=("experience",), saved_profile=saved)(
        Settings()
    )
    try:
        documents, warnings = await client.fetch_profile_documents("example", True)
        assert documents["profile"] == saved
        assert set(documents) == {"profile", "experience"}
        assert calls == ["/in/example/details/experience/"]
        assert any("saved capture" in warning for warning in warnings)
        assert (tmp_path / "section-experience.html").exists()
        with pytest.raises(LinkedInSafetyError):
            await client.fetch_profile_documents("example", True)
        with pytest.raises(LinkedInSafetyError):
            await client._get_once("/in/example/details/education/")
        assert len(calls) == 1
    finally:
        await client.close()


async def test_four_sections_no_main_experience_posts_or_repeat_gets(monkeypatch, tmp_path):
    calls = []

    async def response(self, path, *, accept=None):
        calls.append(path)
        return "<html>synthetic section</html>"

    monkeypatch.setattr(LinkedInClient, "_get_once", response)
    sections = ("education", "skills", "certifications", "languages")
    factory = capture_client(
        "example", tmp_path, sections=(*sections, "education"), saved_profile="<h1>Example</h1>"
    )
    first, second = factory(Settings()), factory(Settings())
    try:
        documents, warnings = await first.fetch_profile_documents("example", True)
        assert set(documents) == {"profile", *sections}
        assert calls == [f"/in/example/details/{section}/" for section in sections]
        assert len(list(tmp_path.iterdir())) == 4
        assert any("saved capture" in warning for warning in warnings)
        for path in (
            "/in/example/",
            "/in/example/details/experience/",
            "/in/example/recent-activity/shares/",
        ):
            with pytest.raises(LinkedInSafetyError):
                await second._get_once(path)
        with pytest.raises(LinkedInSafetyError):
            await second.fetch_profile_documents("example", True)
        with pytest.raises(LinkedInSafetyError):
            await second.fetch_posts_document("example")
        assert len(calls) == 4
    finally:
        await first.close()
        await second.close()


async def test_section_capture_stops_on_failure_and_keeps_completed_files(monkeypatch, tmp_path):
    calls = []

    async def response(self, path, *, accept=None):
        calls.append(path)
        if len(calls) == 2:
            raise LinkedInUpstreamError("Failed")
        return "<html>synthetic section</html>"

    monkeypatch.setattr(LinkedInClient, "_get_once", response)
    client = capture_client(
        "example",
        tmp_path,
        sections=("education", "skills", "certifications", "languages"),
        saved_profile="<h1>Example</h1>",
    )(Settings())
    try:
        with pytest.raises(LinkedInUpstreamError):
            await client.fetch_profile_documents("example", True)
        assert len(calls) == 2
        assert [path.name for path in tmp_path.iterdir()] == ["section-education.html"]
        with pytest.raises(LinkedInSafetyError):
            await client._get_once("/in/example/details/languages/")
        assert len(calls) == 2
    finally:
        await client.close()
