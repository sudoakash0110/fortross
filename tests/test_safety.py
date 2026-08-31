from fortross.safety import MemoryCounterStore, RateLimiter, RateLimitExceeded
from fortross.settings import Settings


async def test_rate_limiter_blocks_after_limit() -> None:
    settings = Settings(
        rate_limit_per_minute=1,
        rate_limit_per_hour=10,
        rate_limit_per_day=20,
    )
    limiter = RateLimiter(settings, MemoryCounterStore())
    await limiter.check("127.0.0.1")
    try:
        await limiter.check("127.0.0.1")
    except RateLimitExceeded:
        pass
    else:
        raise AssertionError("expected RateLimitExceeded")
