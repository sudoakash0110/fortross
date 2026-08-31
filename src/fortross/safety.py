import asyncio
import hashlib
import os
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from fortross.linkedin.errors import LinkedInSafetyError
from fortross.settings import Settings


class RateLimitExceeded(Exception):
    pass


class CounterStore:
    async def increment(self, key: str, window_start: int) -> int:
        raise NotImplementedError

    async def close(self) -> None:
        return None

    async def blocked_until(self, key: str) -> int:
        raise NotImplementedError

    async def block_until(self, key: str, until: int) -> None:
        raise NotImplementedError


class MemoryCounterStore(CounterStore):
    def __init__(self) -> None:
        self._counts: dict[tuple[str, int], int] = defaultdict(int)
        self._lock = asyncio.Lock()
        self._circuits: dict[str, int] = {}

    async def blocked_until(self, key: str) -> int:
        async with self._lock:
            return self._circuits.get(key, 0)

    async def block_until(self, key: str, until: int) -> None:
        async with self._lock:
            self._circuits[key] = max(until, self._circuits.get(key, 0))

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


class SQLiteCounterStore(CounterStore):
    """Atomic local safety state. Durability depends on the host retaining this file."""

    def __init__(self, filename: str) -> None:
        self._path = Path(filename).resolve()

    def _execute(self, sql: str, args: tuple) -> list:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Create privately. Never replace existing state or follow a database symlink.
        descriptor = os.open(self._path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        connection = sqlite3.connect(self._path, timeout=5)
        try:
            with connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS rate_counters ("
                    "key TEXT NOT NULL, window_start INTEGER NOT NULL, count INTEGER NOT NULL, "
                    "PRIMARY KEY (key, window_start))"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS safety_circuits ("
                    "key TEXT PRIMARY KEY, blocked_until INTEGER NOT NULL)"
                )
                return connection.execute(sql, args).fetchall()
        finally:
            connection.close()

    async def increment(self, key: str, window_start: int) -> int:
        rows = await asyncio.to_thread(
            self._execute,
            "INSERT INTO rate_counters(key, window_start, count) VALUES (?, ?, 1) "
            "ON CONFLICT(key, window_start) DO UPDATE SET count = count + 1 RETURNING count",
            (key, window_start),
        )
        return int(rows[0][0])

    async def blocked_until(self, key: str) -> int:
        rows = await asyncio.to_thread(
            self._execute, "SELECT blocked_until FROM safety_circuits WHERE key = ?", (key,)
        )
        return int(rows[0][0]) if rows else 0

    async def block_until(self, key: str, until: int) -> None:
        await asyncio.to_thread(
            self._execute,
            "INSERT INTO safety_circuits(key, blocked_until) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET blocked_until = "
            "MAX(blocked_until, excluded.blocked_until)",
            (key, until),
        )


class TursoCounterStore(CounterStore):
    """Stores only hashed rate-limit keys and counters, never profile data."""

    def __init__(self, url: str, token: str) -> None:
        base = url.replace("libsql://", "https://").rstrip("/")
        parsed = urlsplit(base)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path
        ):
            raise ValueError("TURSO_DATABASE_URL must be an HTTPS or libsql database origin")
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
        payload = response.json()
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or len(results) != len(requests) + 1:
            raise RuntimeError("Unexpected Turso response")
        if any(not isinstance(item, dict) or item.get("type") != "ok" for item in results):
            raise RuntimeError("Turso could not execute safety-state operation")
        return payload

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
                    },
                    {
                        "type": "execute",
                        "stmt": {
                            "sql": (
                                "CREATE TABLE IF NOT EXISTS safety_circuits ("
                                "key TEXT PRIMARY KEY, blocked_until INTEGER NOT NULL)"
                            )
                        },
                    },
                ]
            )
            self._initialized = True

    async def blocked_until(self, key: str) -> int:
        await self._initialize()
        payload = await self._pipeline(
            [
                {
                    "type": "execute",
                    "stmt": {
                        "sql": "SELECT blocked_until FROM safety_circuits WHERE key = ?",
                        "args": [self._arg(key)],
                    },
                }
            ]
        )
        try:
            rows = payload["results"][0]["response"]["result"]["rows"]
            if not isinstance(rows, list):
                raise TypeError
            return int(rows[0][0]["value"]) if rows else 0
        except (KeyError, IndexError, TypeError, ValueError):
            raise RuntimeError("Unexpected Turso circuit response") from None

    async def block_until(self, key: str, until: int) -> None:
        await self._initialize()
        await self._pipeline(
            [
                {
                    "type": "execute",
                    "stmt": {
                        "sql": (
                            "INSERT INTO safety_circuits(key, blocked_until) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET blocked_until = "
                            "MAX(blocked_until, excluded.blocked_until)"
                        ),
                        "args": [self._arg(key), self._arg(until)],
                    },
                }
            ]
        )

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
    def __init__(
        self, settings: Settings, store: CounterStore | None = None, namespace: str = "api"
    ) -> None:
        self.store = store or MemoryCounterStore()
        self.namespace = namespace
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
            try:
                count = await self.store.increment(
                    f"{self.namespace}:{window.name}:{digest}", start
                )
            except Exception:
                raise LinkedInSafetyError("Rate-limit storage is unavailable") from None
            if count > window.limit:
                raise RateLimitExceeded(f"Request limit exceeded for the {window.name} window")


def build_counter_store(settings: Settings) -> CounterStore:
    if settings.turso_database_url and settings.turso_auth_token:
        return TursoCounterStore(settings.turso_database_url, settings.turso_auth_token)
    if settings.safety_state_file:
        return SQLiteCounterStore(settings.safety_state_file)
    return MemoryCounterStore()
