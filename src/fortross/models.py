from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


class DateRange(BaseModel):
    start: date | None = None
    end: date | None = None
    current: bool = False
    display: str | None = None


class Experience(BaseModel):
    title: str | None = None
    company: str | None = None
    employment_type: str | None = None
    location: str | None = None
    date_range: DateRange = Field(default_factory=DateRange)
    description: str | None = None
    company_url: str | None = None
    company_image_url: str | None = None


class Education(BaseModel):
    school: str | None = None
    degree: str | None = None
    field_of_study: str | None = None
    date_range: DateRange = Field(default_factory=DateRange)
    description: str | None = None
    school_url: str | None = None
    school_image_url: str | None = None


class Certification(BaseModel):
    name: str
    issuer: str | None = None
    issued: str | None = None
    expires: str | None = None
    credential_id: str | None = None
    credential_url: str | None = None


class Language(BaseModel):
    name: str
    proficiency: str | None = None


class ProfileImages(BaseModel):
    profile: str | None = None
    background: str | None = None


class LinkedInProfile(BaseModel):
    source_url: HttpUrl
    public_identifier: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{1,99}$")
    name: str | None = Field(default=None, max_length=200)
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    headline: str | None = Field(default=None, max_length=500)
    location: str | None = Field(default=None, max_length=200)
    about: str | None = Field(default=None, max_length=10000)
    images: ProfileImages = Field(default_factory=ProfileImages)
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)


class ProfileRequest(BaseModel):
    url: HttpUrl = Field(
        examples=["https://www.linkedin.com/in/example/"],
        description="LinkedIn profile URL. Replace example with the target profile's slug.",
    )
    include_sections: bool = True


class PostsRequest(BaseModel):
    url: HttpUrl = Field(
        examples=["https://www.linkedin.com/in/example/"],
        description="LinkedIn profile URL. Replace example with the target profile's slug.",
    )
    limit: int = Field(default=50, ge=1, le=50)


class PostAuthor(BaseModel):
    name: str | None = None
    url: str | None = None


class PostContent(BaseModel):
    id: str
    url: str
    text: str | None = None
    published_at: datetime | None = None
    published_at_display: str | None = None
    author: PostAuthor = Field(default_factory=PostAuthor)
    media: list[str] = Field(default_factory=list)


class LinkedInPost(PostContent):
    is_repost: bool | None = None
    original_post_url: str | None = None
    original_post: PostContent | None = None


class PostsResponse(BaseModel):
    request_id: str
    source: Literal["linkedin_internal_web"] = "linkedin_internal_web"
    profile_url: HttpUrl
    scope: Literal["first_page"] = "first_page"
    pagination_followed: bool = False
    has_more: bool | None = None
    truncated: bool = False
    posts: list[LinkedInPost] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ProfileResponse(BaseModel):
    request_id: str
    source: Literal["linkedin_internal_web"] = "linkedin_internal_web"
    profile: LinkedInProfile
    warnings: list[str] = Field(default_factory=list)


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    detail: ErrorBody
