import pytest

from fortross.linkedin.urls import parse_profile_url


def test_accepts_and_canonicalizes_profile_url() -> None:
    target = parse_profile_url("https://linkedin.com/in/Example-Person/")
    assert target.slug == "example-person"
    assert target.canonical_url == "https://www.linkedin.com/in/example-person/"


@pytest.mark.parametrize(
    "value",
    [
        "http://www.linkedin.com/in/example/",
        "https://evil.example/in/example/",
        "https://www.linkedin.com/company/example/",
        "https://www.linkedin.com/in/a%2Fb/",
        "https://www.linkedin.com:444/in/example/",
    ],
)
def test_rejects_non_profile_urls(value: str) -> None:
    with pytest.raises(ValueError):
        parse_profile_url(value)
