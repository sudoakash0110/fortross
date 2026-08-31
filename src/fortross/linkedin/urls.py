import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit

_PROFILE_PATH = re.compile(r"^/in/([^/?#]+)/?$")
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,99}$")


@dataclass(frozen=True)
class ProfileTarget:
    slug: str
    canonical_url: str


def parse_profile_url(value: str) -> ProfileTarget:
    parsed = urlsplit(value.strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or host not in {"linkedin.com", "www.linkedin.com"}:
        raise ValueError("Only HTTPS linkedin.com profile URLs are accepted")
    if parsed.port not in {None, 443}:
        raise ValueError("Only the standard HTTPS port is accepted")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Credentials in profile URLs are not accepted")
    match = _PROFILE_PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError("Expected a LinkedIn URL in the form https://www.linkedin.com/in/<slug>/")
    slug = unquote(match.group(1)).lower()
    if not _SLUG.fullmatch(slug):
        raise ValueError("The LinkedIn public identifier is invalid")
    canonical = urlunsplit(("https", "www.linkedin.com", f"/in/{slug}/", "", ""))
    return ProfileTarget(slug=slug, canonical_url=canonical)
