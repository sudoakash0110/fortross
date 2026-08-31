from pydantic import ValidationError

from fortross.linkedin.client import LinkedInClient
from fortross.linkedin.errors import (
    LinkedInConfigurationError,
    LinkedInDisabledError,
    ParseError,
    ProfileNotAllowedError,
)
from fortross.linkedin.feed import parse_posts_feed
from fortross.linkedin.parser import parse_profile_documents
from fortross.linkedin.posts import parse_posts_document
from fortross.linkedin.urls import ProfileTarget
from fortross.linkedin.voyager import has_voyager_bootstrap
from fortross.models import LinkedInPost, LinkedInProfile
from fortross.settings import Settings


class ProfileService:
    def __init__(self, settings: Settings, client: LinkedInClient | None = None) -> None:
        self.settings = settings
        self.client = client or LinkedInClient(settings)

    def authorize(self, target: ProfileTarget) -> None:
        if not self.settings.linkedin_live_enabled:
            raise LinkedInDisabledError("Live LinkedIn access is disabled")
        try:
            self.settings.validate_live_configuration()
        except ValueError as exc:
            raise LinkedInConfigurationError(str(exc)) from exc
        allowed = set(self.settings.allowed_profile_slugs)
        if self.settings.linkedin_profile_access == "allowlist" and target.slug not in allowed:
            raise ProfileNotAllowedError("This profile is not in ALLOWED_PROFILE_SLUGS")

    async def fetch(
        self, target: ProfileTarget, include_sections: bool = True
    ) -> tuple[LinkedInProfile, list[str]]:
        self.authorize(target)
        documents, warnings = await self.client.fetch_profile_documents(
            target.slug, include_sections
        )
        try:
            profile = parse_profile_documents(target.slug, documents, include_sections, warnings)
        except (ValidationError, RecursionError) as exc:
            raise ParseError("LinkedIn profile data failed structural validation") from exc
        if not profile.name:
            raise ParseError("LinkedIn did not expose a recognizable profile identity")
        for field in ("headline", "location", "about"):
            if not getattr(profile, field):
                warnings.append(f"Profile {field} is unavailable or could not be parsed reliably")
        for section in ("experience", "education", "skills", "certifications", "languages"):
            if section in documents and not getattr(profile, section):
                warnings.append(
                    f"The {section} document was fetched, but no items were recognized; "
                    "the section may be empty, loaded separately, or unsupported by the parser"
                )
        return profile, warnings

    async def fetch_posts(
        self, target: ProfileTarget, limit: int = 50
    ) -> tuple[list[LinkedInPost], bool, list[str]]:
        self.authorize(target)
        if not 1 <= limit <= 50:
            raise ValueError("Posts limit must be between 1 and 50")
        document = await self.client.fetch_posts_document(target.slug)
        if has_voyager_bootstrap(document):
            feed = await self.client.fetch_posts_feed_from_bootstrap(document, target.slug, limit)
            return parse_posts_feed(feed, limit)
        return parse_posts_document(document, limit)
