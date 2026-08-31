import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from fortross.api import failure, router
from fortross.safety import RateLimiter, build_counter_store
from fortross.sessions import SessionManager
from fortross.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.app_env == "production":
        settings.validate_server_configuration()
    store = build_counter_store(settings)
    manager = SessionManager(settings, store)
    app.state.session_manager = manager
    app.state.rate_limiter = RateLimiter(settings, store)
    cleanup = asyncio.create_task(manager.cleanup_loop())
    logger = logging.getLogger("fortross.audit")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)
    try:
        yield
    finally:
        cleanup.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup
        await manager.close()
        await store.close()


app = FastAPI(
    title="Fortross LinkedIn Profile API",
    version="0.2.0",
    description=(
        "Paste your full raw LinkedIn Cookie value into the cookie form field of "
        "POST /login-with-cookie. Keep quotes unchanged; no JSON escaping is needed. "
        "No API key or credential file is required. Copy the returned access_token, "
        "click Authorize, and enter the raw token in SessionToken. "
        "Use /profiles or /posts, then POST /logout to permanently revoke that token. "
        "Cookies are held in server memory for the session. Login does not verify them "
        "with LinkedIn. Automated requests can put the supplied LinkedIn account at risk. "
        "Do not share cookies or tokens, including screenshots of request/response bodies."
    ),
    docs_url="/playground",
    swagger_ui_oauth2_redirect_url="/playground/oauth2-redirect",
    lifespan=lifespan,
    swagger_ui_parameters={"persistAuthorization": False},
)
app.include_router(router)


@app.get("/docs", include_in_schema=False)
async def legacy_docs():
    return RedirectResponse("/playground")


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # Default validation errors can include the complete rejected cookie value.
    return JSONResponse(
        status_code=422,
        content={
            "detail": failure(
                "invalid_request", "Request body or parameters failed validation", 422
            ).detail
        },
        headers={"Cache-Control": "no-store"},
    )


@app.middleware("http")
async def no_cache(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


class BodyLimit:
    """Bound request bodies before JSON parsing, including chunked login requests."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > 100_000:
                response = JSONResponse(
                    status_code=413,
                    content={
                        "detail": failure(
                            "request_too_large", "Request body exceeds 100 KB", 413
                        ).detail
                    },
                    headers={"Cache-Control": "no-store"},
                )
                return await response(scope, receive, send)
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


app.add_middleware(BodyLimit)
