from contextlib import asynccontextmanager

from fastapi import FastAPI

from fortross.api import router
from fortross.linkedin.client import LinkedInClient, UpstreamGovernor
from fortross.safety import RateLimiter, build_counter_store
from fortross.service import ProfileService
from fortross.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    counter_store = build_counter_store(settings)
    governor = UpstreamGovernor(settings, counter_store)
    app.state.profile_service = ProfileService(settings, LinkedInClient(settings, governor))
    app.state.rate_limiter = RateLimiter(settings, counter_store)
    try:
        yield
    finally:
        await app.state.profile_service.client.close()
        await counter_store.close()


app = FastAPI(
    title="Fortross LinkedIn Profile API",
    version="0.1.0",
    description="Browserless, safety-first LinkedIn profile extraction assignment.",
    lifespan=lifespan,
)
app.include_router(router)
