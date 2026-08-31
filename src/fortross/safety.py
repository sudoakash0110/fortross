import asyncio
import hashlib
import time
from collections import defaultdict
from dataclasses import dataclass

import httpx

from fortross.settings import Settings


class RateLimitExceeded(Exception):
    pass


class CounterStore:
    async def increment(self, key: str, window_start: int) -> int:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class MemoryCounterStore(CounterStore):
    def __init__(self) -> None:
        self._counts: dict[tuple[str, int], int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def increment(self, key: str, window_start: int) -> int:
        async with self._lock:
            composite = (key, window_start)
            self._counts[composite] += 1
            if len(self._counts) > 10_000:
                floor = int(time.time()) - 172_800
                self._counts = defaultdict(
                    int, {item: value for item, value in self._counts.items() if item[1] > floor}
                )
            return self._counts[composite]


class TursoCounterStore(CounterStore):
    """Stores only hashed rate-limit keys and counters, never profile data."""

    def __init__(self, url: str, token: str) -> None:
        base = url.replace("libsql://", "https://").rstrip("/")
        self._url = f"{base}/v2/pipeline"
        self._client = httpx.AsyncClient(
            headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
            timeout=10,
        )
        self._initialized = False
        self._init_lock = asyncio.Lock()

    @staticmethod
    def _arg(value: str | int) -> dict[str, str]:
        return {"type": "integer" if isinstance(value, int) else "text", "value": str(value)}

    async def _pipeline(self, requests: list[dict[str, object]]) -> dict[str, object]:
        response = await self._client.post(
            self._url, json={"requests": [*requests, {"type": "close"}]}
        )
        response.raise_for_status()
        return response.json()

    async def _initialize(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            await self._pipeline(
                [
                    {
                        "type": "execute",
                        "stmt": {
                            "sql": (
                                "CREATE TABLE IF NOT EXISTS rate_counters ("
                                "key TEXT NOT NULL, window_start INTEGER NOT NULL, "
                                "count INTEGER NOT NULL, "
                                "PRIMARY KEY (key, window_start))"
                            )
                        },
                    }
                ]
            )
            self._initialized = True

    async def increment(self, key: str, window_start: int) -> int:
        await self._initialize()
        payload = await self._pipeline(
            [
                {
                    "type": "execute",
                    "stmt": {
                        "sql": (
                            "INSERT INTO rate_counters(key, window_start, count) VALUES (?, ?, 1) "
                            "ON CONFLICT(key, window_start) DO UPDATE SET count = count + 1 "
                            "RETURNING count"
                        ),
                        "args": [self._arg(key), self._arg(window_start)],
                    },
                }
            ]
        )
        try:
            value = payload["results"][0]["response"]["result"]["rows"][0][0]["value"]
            return int(value)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError("Unexpected Turso response") from exc

    async def close(self) -> None:
        await self._client.aclose()


@dataclass(frozen=True)
class Window:
    name: str
    seconds: int
    limit: int


class RateLimiter:
    def __init__(self, settings: Settings, store: CounterStore | None = None) -> None:
        self.store = store or MemoryCounterStore()
        self.windows = (
            Window("minute", 60, settings.rate_limit_per_minute),
            Window("hour", 3600, settings.rate_limit_per_hour),
            Window("day", 86_400, settings.rate_limit_per_day),
        )

    async def check(self, identity: str) -> None:
        digest = hashlib.sha256(identity.encode()).hexdigest()
        now = int(time.time())
        for window in self.windows:
            start = now - (now % window.seconds)
            count = await self.store.increment(f"{window.name}:{digest}", start)
            if count > window.limit:
                raise RateLimitExceeded(f"Request limit exceeded for the {window.name} window")


def build_counter_store(settings: Settings) -> CounterStore:
    if settings.turso_database_url and settings.turso_auth_token:
        return TursoCounterStore(settings.turso_database_url, settings.turso_auth_token)
    return MemoryCounterStore()
