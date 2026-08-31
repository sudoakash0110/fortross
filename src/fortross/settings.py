from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from fortross.cookies import read_cookie_file


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    app_env: Literal["development", "production", "test"] = "development"

    linkedin_live_enabled: bool = False
    linkedin_profile_access: Literal["allowlist", "any"] = "allowlist"
    linkedin_li_at: str = Field(default="", repr=False, exclude=True)
    linkedin_jsessionid: str = Field(default="", repr=False, exclude=True)
    linkedin_cookie_file: str = Field(default="", repr=False, exclude=True)
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
    turso_auth_token: str = Field(default="", repr=False, exclude=True)
    safety_state_file: str = ""
    session_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    max_active_sessions: int = Field(default=20, ge=1, le=100)
    login_limit_per_minute: int = Field(default=5, ge=1, le=60)
    linkedin_global_max_requests_per_hour: int = Field(default=60, ge=1)
    linkedin_global_max_requests_per_day: int = Field(default=120, ge=1)

    @model_validator(mode="after")
    def load_session_file(self):
        # An explicit file is authoritative. Never silently fall back to stale env cookies.
        if self.linkedin_cookie_file:
            self.linkedin_li_at, self.linkedin_jsessionid = read_cookie_file(
                self.linkedin_cookie_file
            )
        for value in (
            self.linkedin_li_at,
            self.linkedin_jsessionid,
            self.turso_auth_token,
        ):
            if any(ord(char) < 32 or ord(char) > 126 for char in value):
                raise ValueError("Credential values must contain printable ASCII only")
        if bool(self.turso_database_url) != bool(self.turso_auth_token):
            raise ValueError("Set both TURSO_DATABASE_URL and TURSO_AUTH_TOKEN, or neither")
        return self

    @field_validator("allowed_profile_slugs", mode="before")
    @classmethod
    def split_slugs(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip().lower() for part in value.split(",") if part.strip())
        return value

    @field_validator("turso_database_url")
    @classmethod
    def database_origin(cls, value: str) -> str:
        if value:
            try:
                parsed = urlsplit(value.replace("libsql://", "https://").rstrip("/"))
                valid = (
                    parsed.scheme == "https"
                    and parsed.hostname
                    and not parsed.username
                    and not parsed.password
                    and not parsed.query
                    and not parsed.fragment
                    and not parsed.path
                )
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("TURSO_DATABASE_URL must be an HTTPS or libsql database origin")
        return value

    def validate_live_configuration(self) -> None:
        self.validate_server_configuration()
        missing = []
        if not self.linkedin_li_at:
            missing.append("LINKEDIN_LI_AT")
        if not self.linkedin_jsessionid:
            missing.append("LINKEDIN_JSESSIONID")
        if missing:
            raise ValueError(f"Live mode is missing required settings: {', '.join(missing)}")

    def validate_server_configuration(self) -> None:
        if self.linkedin_profile_access == "allowlist" and not self.allowed_profile_slugs:
            raise ValueError("ALLOWED_PROFILE_SLUGS is required in allowlist mode")
        if self.app_env == "production":
            if not self.turso_database_url and not self.safety_state_file:
                raise ValueError("Production live mode requires SAFETY_STATE_FILE or Turso")
            if self.linkedin_max_concurrency != 1 or self.linkedin_max_retries != 0:
                raise ValueError("Production requires concurrency 1 and retries 0")


@lru_cache
def get_settings() -> Settings:
    # API processes must NEVER load an operator's LinkedIn account, even if a legacy
    # .env or cookie file is present. Only login request cookies create API clients.
    return Settings(linkedin_li_at="", linkedin_jsessionid="", linkedin_cookie_file="")
