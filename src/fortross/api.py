import hmac
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import ValidationError

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
from fortross.settings import Settings, get_settings

router = APIRouter()


def get_service(request: Request) -> ProfileService:
    return request.app.state.profile_service


def get_rate_limiter(request: Request) -> RateLimiter:
    return request.app.state.rate_limiter


SettingsDependency = Annotated[Settings, Depends(get_settings)]
ServiceDependency = Annotated[ProfileService, Depends(get_service)]
LimiterDependency = Annotated[RateLimiter, Depends(get_rate_limiter)]
ApiKeyHeader = Annotated[str | None, Header()]


def require_api_key(settings: SettingsDependency, x_api_key: ApiKeyHeader = None) -> None:
    if (
        not settings.api_key
        or not x_api_key
        or not hmac.compare_digest(x_api_key, settings.api_key)
    ):
        raise HTTPException(
            status_code=401,
            detail=ErrorBody(
                code="invalid_api_key",
                message="A valid X-API-Key header is required",
                request_id=str(uuid.uuid4()),
            ).model_dump(),
        )


@router.get("/healthz")
async def health(settings: SettingsDependency) -> dict[str, object]:
    return {"status": "ok", "live_linkedin_enabled": settings.linkedin_live_enabled}


@router.post(
    "/v1/profiles",
    response_model=ProfileResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
    dependencies=[Depends(require_api_key)],
)
async def get_profile(
    payload: ProfileRequest,
    request: Request,
    service: ServiceDependency,
    limiter: LimiterDependency,
) -> ProfileResponse:
    request_id = str(uuid.uuid4())
    client_ip = request.client.host if request.client else "unknown"
    try:
        await limiter.check(client_ip)
        target = parse_profile_url(str(payload.url))
        profile, warnings = await service.fetch(target, payload.include_sections)
        return ProfileResponse(request_id=request_id, profile=profile, warnings=warnings)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(
            status_code=400,
            detail=ErrorBody(
                code="invalid_profile_url", message=str(exc), request_id=request_id
            ).model_dump(),
        ) from exc
    except RateLimitExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail=ErrorBody(
                code="api_rate_limited", message=str(exc), request_id=request_id
            ).model_dump(),
        ) from exc
    except LinkedInError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=ErrorBody(code=exc.code, message=str(exc), request_id=request_id).model_dump(),
        ) from exc


@router.post(
    "/v1/posts",
    response_model=PostsResponse,
    responses={status: {"model": ErrorResponse} for status in (400, 401, 403, 404, 429, 502, 503)},
    dependencies=[Depends(require_api_key)],
)
async def get_posts(
    payload: PostsRequest,
    request: Request,
    service: ServiceDependency,
    limiter: LimiterDependency,
) -> PostsResponse:
    request_id = str(uuid.uuid4())
    client_ip = request.client.host if request.client else "unknown"
    try:
        await limiter.check(client_ip)
        target = parse_profile_url(str(payload.url))
        posts, truncated, warnings = await service.fetch_posts(target, payload.limit)
        return PostsResponse(
            request_id=request_id,
            profile_url=target.canonical_url,
            posts=posts,
            truncated=truncated,
            warnings=warnings,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=ErrorBody(
                code="invalid_profile_url",
                message=str(exc),
                request_id=request_id,
            ).model_dump(),
        ) from exc
    except RateLimitExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail=ErrorBody(
                code="api_rate_limited",
                message=str(exc),
                request_id=request_id,
            ).model_dump(),
        ) from exc
    except LinkedInError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=ErrorBody(code=exc.code, message=str(exc), request_id=request_id).model_dump(),
        ) from exc
