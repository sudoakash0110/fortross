import json
import sqlite3
import time

import httpx
import pytest

from fortross.linkedin.client import UpstreamGovernor
from fortross.linkedin.errors import CircuitOpenError, LinkedInSafetyError
from fortross.safety import MemoryCounterStore, RateLimiter, TursoCounterStore
from fortross.settings import Settings


@pytest.fixture
def database():
    connection = sqlite3.connect(":memory:")
    yield connection
    connection.close()


async def store_for(database):
    """Execute the real SQL locally behind a synthetic Turso HTTP response."""

    def handler(request):
        assert request.url == "https://example.turso.io/v2/pipeline"
        responses = []
        for operation in json.loads(request.content)["requests"]:
            if operation["type"] == "close":
                responses.append({"type": "ok", "response": {"type": "close"}})
                continue
            stmt = operation["stmt"]
            args = [
                int(v["value"]) if v["type"] == "integer" else v["value"]
                for v in stmt.get("args", [])
            ]
            cursor = database.execute(stmt["sql"], args)
            rows = [
                [{"type": "integer", "value": str(v)} for v in row] for row in cursor.fetchall()
            ]
            responses.append({"type": "ok", "response": {"result": {"rows": rows}}})
        database.commit()
        return httpx.Response(200, json={"results": responses})

    store = TursoCounterStore("libsql://example.turso.io", "synthetic-token")
    await store._client.aclose()
    store._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return store


async def test_budget_and_circuit_survive_new_client_instances(database):
    first, second = await store_for(database), await store_for(database)
    settings = Settings(linkedin_li_at="synthetic-session", linkedin_max_requests_per_hour=1)
    try:
        governor = UpstreamGovernor(settings, first)
        async with governor.slot():
            pass
        restarted = UpstreamGovernor(settings, second)
        with pytest.raises(LinkedInSafetyError, match="budget exhausted"):
            async with restarted.slot():
                pytest.fail("Restart must not reset counters")
        await governor.trip()
        with pytest.raises(CircuitOpenError):
            async with UpstreamGovernor(settings, second).slot():
                pytest.fail("Restart must not reset circuit")
        rows = database.execute("SELECT key FROM safety_circuits").fetchall()
        assert all("synthetic-session" not in row[0] for row in rows)
    finally:
        await first.close()
        await second.close()


async def test_persistent_block_cannot_shorten_and_can_expire(database):
    store = await store_for(database)
    try:
        await store.block_until("example", 100)
        await store.block_until("example", 50)
        assert await store.blocked_until("example") == 100
        assert await store.blocked_until("missing") == 0
        governor = UpstreamGovernor(Settings(), store)
        await store.block_until(governor._identity, int(time.time()) - 1)
        async with governor.slot():
            pass
    finally:
        await store.close()


async def test_sql_error_inside_http_200_fails_closed():
    store = TursoCounterStore("https://example.turso.io", "fake")
    await store._client.aclose()
    store._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"results": [{"type": "error"}]})
        )
    )
    try:
        with pytest.raises(RuntimeError):
            await store.increment("example", 0)
        assert not store._initialized
    finally:
        await store.close()


async def test_storage_failure_blocks_requests_and_keeps_local_circuit_open():
    class BrokenStore(MemoryCounterStore):
        async def blocked_until(self, key):
            raise RuntimeError("private database details")

        async def block_until(self, key, until):
            raise RuntimeError("private database details")

        async def increment(self, key, window_start):
            raise RuntimeError("private database details")

    store = BrokenStore()
    governor = UpstreamGovernor(Settings(), store)
    with pytest.raises(LinkedInSafetyError, match="storage is unavailable"):
        async with governor.slot():
            pytest.fail("Must not reach LinkedIn")
    with pytest.raises(LinkedInSafetyError, match="access paused"):
        await governor.trip()
    with pytest.raises(CircuitOpenError):
        async with governor.slot():
            pytest.fail("Local circuit remains open")
    with pytest.raises(LinkedInSafetyError, match="storage is unavailable"):
        await RateLimiter(Settings(), store).check("example")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:secret@example.com",
        "https://example.com/path",
        "https://example.com?token=secret",
    ],
)
def test_turso_requires_clean_https_origin(url):
    with pytest.raises(ValueError, match="database origin"):
        TursoCounterStore(url, "fake")
