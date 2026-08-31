import logging
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, SecretStr

from fortross.linkedin.errors import LinkedInError
from fortross.linkedin.urls import parse_profile_url
from fortross.models import (
    ErrorBody,
    ErrorResponse,
    PostsRequest,
    PostsResponse,
    ProfileRequest,
    ProfileResponse,
)
from fortross.safety import RateLimiter, RateLimitExceeded
from fortross.service import ProfileService
from fortross.sessions import APISession, SessionManager
from fortross.settings import Settings, get_settings

router = APIRouter()
audit = logging.getLogger("fortross.audit")
bearer_header = HTTPBearer(auto_error=False, scheme_name="SessionToken")


class LoginResponse(BaseModel):
    access_token: str = Field(repr=False)
    token_type: Literal["bearer"] = "bearer"
    session_id: str
    expires_in: int
    expires_at: datetime
    linkedin_session_verified: Literal[False] = False


def failure(code: str, message: str, status: int, request_id: str | None = None) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ErrorBody(
            code=code, message=message, request_id=request_id or str(uuid.uuid4())
        ).model_dump(),
    )


def get_manager(request: Request) -> SessionManager:
    return request.app.state.session_manager


def get_rate_limiter(request: Request) -> RateLimiter:
    return request.app.state.rate_limiter


SettingsDependency = Annotated[Settings, Depends(get_settings)]
ManagerDependency = Annotated[SessionManager, Depends(get_manager)]
LimiterDependency = Annotated[RateLimiter, Depends(get_rate_limiter)]


async def require_session(
    manager: ManagerDependency,
    authorization: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_header)],
) -> APISession:
    if authorization is None:
        raise failure("invalid_session", "A bearer session token is required", 401)
    try:
        return await manager.resolve(authorization.credentials)
    except LinkedInError as exc:
        raise failure(exc.code, str(exc), exc.status_code) from None


SessionDependency = Annotated[APISession, Depends(require_session)]


def get_service(session: SessionDependency) -> ProfileService:
    return session.service


ServiceDependency = Annotated[ProfileService, Depends(get_service)]


async def limit_login(
    request: Request,
    manager: ManagerDependency,
) -> None:
    try:
        await manager.login_limiter.check("global")
        await manager.login_limiter.check(request.client.host if request.client else "unknown")
    except RateLimitExceeded:
        raise failure("login_rate_limited", "Login request limit exceeded", 429) from None
    except LinkedInError as exc:
        raise failure(exc.code, str(exc), exc.status_code) from None


@router.get("/healthz")
async def health(settings: SettingsDependency) -> dict[str, object]:
    return {"status": "ok", "live_linkedin_enabled": settings.linkedin_live_enabled}


@router.post(
    "/login-with-cookie", dependencies=[Depends(limit_login)], response_model=LoginResponse
)
async def login(
    manager: ManagerDependency,
    cookie: Annotated[
        SecretStr,
        Form(
            min_length=1,
            max_length=65_536,
            description="Paste your full raw LinkedIn Cookie value, including quotes. "
            "Do not JSON-escape it. Only li_at and JSESSIONID are retained for this session.",
        ),
    ],
):
    try:
        token, session = await manager.create(cookie.get_secret_value())
    except ValueError:
        raise failure(
            "invalid_cookie", "Provide one Cookie header containing li_at and JSESSIONID", 400
        ) from None
    except LinkedInError as exc:
        raise failure(exc.code, str(exc), exc.status_code) from None
    return {
        "access_token": token,
        "token_type": "bearer",
        "session_id": session.id,
        "expires_in": manager.settings.session_ttl_seconds,
        "expires_at": session.expires_at,
        "linkedin_session_verified": False,
    }


@router.post("/logout")
async def logout(session: SessionDependency, manager: ManagerDependency):
    await manager.revoke(session)
    return {"logged_out": True}


@router.post(
    "/v1/profiles",
    include_in_schema=False,
    response_model=ProfileResponse,
)
@router.post(
    "/profiles",
    response_model=ProfileResponse,
    responses={s: {"model": ErrorResponse} for s in (400, 401, 403, 429, 502, 503)},
)
async def get_profile(
    payload: ProfileRequest,
    service: ServiceDependency,
    limiter: LimiterDependency,
    session: SessionDependency,
    manager: ManagerDependency,
) -> ProfileResponse:
    request_id = str(uuid.uuid4())
    try:
        await limiter.check(session.account_key)
        target = parse_profile_url(str(payload.url))
        profile, warnings = await manager.run(
            session, lambda: service.fetch(target, payload.include_sections)
        )
        audit.info("profile_ok session_id=%s request_id=%s", session.id, request_id)
        return ProfileResponse(request_id=request_id, profile=profile, warnings=warnings)
    except ValueError:
        raise failure(
            "invalid_profile_url", "Provide a valid LinkedIn profile URL", 400, request_id
        ) from None
    except RateLimitExceeded:
        raise failure("api_rate_limited", "API request limit exceeded", 429, request_id) from None
    except LinkedInError as exc:
        audit.info(
            "profile_failed session_id=%s request_id=%s code=%s", session.id, request_id, exc.code
        )
        raise failure(exc.code, str(exc), exc.status_code, request_id) from None


@router.post(
    "/v1/posts",
    include_in_schema=False,
    response_model=PostsResponse,
)
@router.post(
    "/posts",
    response_model=PostsResponse,
    responses={s: {"model": ErrorResponse} for s in (400, 401, 403, 429, 502, 503)},
)
async def get_posts(
    payload: PostsRequest,
    service: ServiceDependency,
    limiter: LimiterDependency,
    session: SessionDependency,
    manager: ManagerDependency,
) -> PostsResponse:
    request_id = str(uuid.uuid4())
    try:
        await limiter.check(session.account_key)
        target = parse_profile_url(str(payload.url))
        posts, truncated, warnings = await manager.run(
            session, lambda: service.fetch_posts(target, payload.limit)
        )
        audit.info("posts_ok session_id=%s request_id=%s", session.id, request_id)
        return PostsResponse(
            request_id=request_id,
            profile_url=target.canonical_url,
            posts=posts,
            truncated=truncated,
            warnings=warnings,
        )
    except ValueError:
        raise failure(
            "invalid_profile_url", "Provide a valid LinkedIn profile URL", 400, request_id
        ) from None
    except RateLimitExceeded:
        raise failure("api_rate_limited", "API request limit exceeded", 429, request_id) from None
    except LinkedInError as exc:
        audit.info(
            "posts_failed session_id=%s request_id=%s code=%s", session.id, request_id, exc.code
        )
        raise failure(exc.code, str(exc), exc.status_code, request_id) from None
