from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    api_key: str = ""

    linkedin_live_enabled: bool = False
    linkedin_profile_access: Literal["allowlist", "any"] = "allowlist"
    linkedin_li_at: str = ""
    linkedin_jsessionid: str = ""
    allowed_profile_slugs: Annotated[tuple[str, ...], NoDecode] = ()

    rate_limit_per_minute: int = Field(default=2, ge=1)
    rate_limit_per_hour: int = Field(default=10, ge=1)
    rate_limit_per_day: int = Field(default=20, ge=1)
    linkedin_max_concurrency: int = Field(default=1, ge=1, le=2)
    linkedin_min_request_interval_seconds: float = Field(default=2.0, ge=0.5)
    linkedin_request_timeout_seconds: float = Field(default=20.0, ge=5, le=60)
    linkedin_max_retries: int = Field(default=0, ge=0, le=1)
    linkedin_posts_query_id: str = Field(
        default="voyagerFeedDashProfileUpdates.20c70fe0314184158516a7ec004c0408",
        pattern=r"^voyagerFeedDashProfileUpdates\.[a-f0-9]{32}$",
    )
    linkedin_max_requests_per_hour: int = Field(default=30, ge=1)
    linkedin_max_requests_per_day: int = Field(default=60, ge=1)
    linkedin_max_response_bytes: int = Field(default=5_000_000, ge=100_000, le=20_000_000)
    linkedin_circuit_breaker_cooldown_seconds: int = Field(default=3600, ge=60)
    linkedin_user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"
    )

    turso_database_url: str = ""
    turso_auth_token: str = ""

    @field_validator("allowed_profile_slugs", mode="before")
    @classmethod
    def split_slugs(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip().lower() for part in value.split(",") if part.strip())
        return value

    def validate_live_configuration(self) -> None:
        missing = []
        if not self.api_key:
            missing.append("API_KEY")
        if not self.linkedin_li_at:
            missing.append("LINKEDIN_LI_AT")
        if not self.linkedin_jsessionid:
            missing.append("LINKEDIN_JSESSIONID")
        if self.linkedin_profile_access == "allowlist" and not self.allowed_profile_slugs:
            missing.append("ALLOWED_PROFILE_SLUGS")
        if missing:
            raise ValueError(f"Live mode is missing required settings: {', '.join(missing)}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
