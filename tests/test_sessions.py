import asyncio
import time

import httpx
import pytest

from fortross.linkedin.errors import (
    CircuitOpenError,
    LinkedInSafetyError,
    SessionAuthError,
    SessionCapacityError,
)
from fortross.safety import MemoryCounterStore, SQLiteCounterStore
from fortross.sessions import SessionManager
from fortross.settings import Settings


@pytest.fixture
async def manager():
    settings = Settings(
        api_key="invite",
        linkedin_live_enabled=True,
        linkedin_profile_access="any",
        linkedin_min_request_interval_seconds=0.5,
    )
    value = SessionManager(settings, MemoryCounterStore())
    yield value
    await value.close()


async def mock_client(session, handler):
    client = session.service.client
    headers, cookies = client._client.headers.copy(), client._client.cookies
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://www.linkedin.com",
        headers=headers,
        cookies=cookies,
        transport=httpx.MockTransport(handler),
    )
    client.governor._interval = 0


async def test_each_token_uses_only_its_cookie_and_no_raw_token_is_retained(manager):
    tokens = []
    sessions = []
    for name in ("alice", "bob"):
        token, session = await manager.create(
            f"li_at={name}; JSESSIONID=ajax:{name}; other=discard"
        )
        tokens.append(token)
        sessions.append(session)

        def handler(request, expected=name):
            assert request.headers["cookie"] == f'li_at={expected}; JSESSIONID="ajax:{expected}"'
            assert request.headers["csrf-token"] == f"ajax:{expected}"
            return httpx.Response(200, text="ok")

        await mock_client(session, handler)
    for token, session in zip(tokens, sessions, strict=True):
        assert await manager.resolve(token) is session
        assert token not in manager._sessions
        assert len(session.token_hash) == 64
        assert (
            await manager.run(session, lambda s=session: s.service.client._get_once("/in/example/"))
            == "ok"
        )
    assert tokens[0] != tokens[1]
    await manager.revoke(sessions[0])
    with pytest.raises(SessionAuthError):
        await manager.resolve(tokens[0])
    assert await manager.resolve(tokens[1]) is sessions[1]
    assert sessions[0].service.settings.linkedin_li_at == ""
    assert len(sessions[0].service.client._client.cookies) == 0


async def test_expiry_disposes_cookie_and_capacity_is_bounded(manager):
    manager.settings.max_active_sessions = 1
    token, session = await manager.create("li_at=alice; JSESSIONID=ajax:alice")
    with pytest.raises(SessionCapacityError):
        await manager.create("li_at=bob; JSESSIONID=ajax:bob")
    session.deadline = time.monotonic() - 1
    with pytest.raises(SessionAuthError):
        await manager.resolve(token)
    assert session.service.client.settings.linkedin_li_at == ""
    await manager.create("li_at=bob; JSESSIONID=ajax:bob")


async def test_reaper_clears_expired_sessions_without_another_request(manager):
    _, session = await manager.create("li_at=alice; JSESSIONID=ajax:alice")
    session.deadline = 0
    await manager.reap()
    assert not manager._sessions
    assert session.revoked and session.service.client._client.is_closed


async def test_relogin_does_not_reset_account_budget(manager):
    manager.settings.linkedin_max_requests_per_hour = 1
    _, first = await manager.create("li_at=alice; JSESSIONID=ajax:alice")
    async with first.service.client.governor.slot():
        pass
    await manager.revoke(first)
    _, second = await manager.create("li_at=alice; JSESSIONID=ajax:changed")
    second.service.client.governor._interval = 0
    with pytest.raises(LinkedInSafetyError, match="budget exhausted"):
        async with second.service.client.governor.slot():
            pytest.fail("Re-login must not reset the budget")


async def test_logout_does_not_clear_another_tokens_same_cookie(manager):
    _, first = await manager.create("li_at=alice; JSESSIONID=ajax:alice")
    token, second = await manager.create("li_at=alice; JSESSIONID=ajax:alice")
    await manager.revoke(first)
    assert await manager.resolve(token) is second
    assert second.service.settings.linkedin_li_at == "alice"
    assert second.service.client._client.cookies.get("li_at") == "alice"


async def test_auth_rejection_revokes_even_when_circuit_persistence_fails(manager):
    async def broken_write(key, until):
        raise RuntimeError("offline storage failure")

    manager.store.block_until = broken_write
    token, session = await manager.create("li_at=alice; JSESSIONID=ajax:alice")
    await mock_client(session, lambda _: httpx.Response(401))
    with pytest.raises(SessionAuthError):
        await manager.run(session, lambda: session.service.client._get_once("/in/example/"))
    with pytest.raises(SessionAuthError):
        await manager.resolve(token)


async def test_auth_failure_revokes_same_cookie_tokens_only(manager):
    token_a, first = await manager.create("li_at=alice; JSESSIONID=ajax:alice")
    token_a2, second = await manager.create("li_at=alice; JSESSIONID=ajax:alice")
    token_b, other = await manager.create("li_at=bob; JSESSIONID=ajax:bob")
    assert first.service.client.governor is second.service.client.governor
    await mock_client(first, lambda _: httpx.Response(403))
    with pytest.raises(SessionAuthError):
        await manager.run(first, lambda: first.service.client._get_once("/in/example/"))
    for token in (token_a, token_a2):
        with pytest.raises(SessionAuthError):
            await manager.resolve(token)
    assert await manager.resolve(token_b) is other
    _, again = await manager.create("li_at=alice; JSESSIONID=ajax:alice")
    with pytest.raises(CircuitOpenError):
        async with again.service.client.governor.slot():
            pytest.fail("Re-login must not bypass checkpoint cooldown")


async def test_logout_cancels_inflight_and_queued_requests(manager):
    _, first = await manager.create("li_at=alice; JSESSIONID=ajax:alice")
    token_b, second = await manager.create("li_at=bob; JSESSIONID=ajax:bob")
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def first_handler(request):
        calls.append("alice")
        entered.set()
        await release.wait()
        return httpx.Response(200, text="ok")

    def second_handler(request):
        calls.append("bob")
        return httpx.Response(200, text="ok")

    await mock_client(first, first_handler)
    await mock_client(second, second_handler)
    running = asyncio.create_task(
        manager.run(first, lambda: first.service.client._get_once("/in/example/"))
    )
    await entered.wait()
    queued = asyncio.create_task(
        manager.run(second, lambda: second.service.client._get_once("/in/example/"))
    )
    await asyncio.sleep(0)
    await manager.revoke(second)
    with pytest.raises(SessionAuthError):
        await queued
    assert calls == ["alice"]
    with pytest.raises(SessionAuthError):
        await manager.resolve(token_b)
    await manager.revoke(first)
    with pytest.raises(SessionAuthError):
        await running


async def test_global_budget_applies_across_different_cookies(manager):
    manager.settings.linkedin_global_max_requests_per_hour = 1
    _, first = await manager.create("li_at=alice; JSESSIONID=ajax:a")
    _, second = await manager.create("li_at=bob; JSESSIONID=ajax:b")
    async with first.service.client.governor.slot():
        pass
    second.service.client.governor._interval = 0
    with pytest.raises(LinkedInSafetyError, match="Global upstream"):
        async with second.service.client.governor.slot():
            pytest.fail("New credentials must not bypass global limits")


async def test_restart_invalidates_tokens_and_sqlite_contains_no_credentials(tmp_path):
    path = tmp_path / "safety.sqlite3"
    settings = Settings(api_key="invite", linkedin_profile_access="any")
    first = SessionManager(settings, SQLiteCounterStore(str(path)))
    token, session = await first.create("li_at=synthetic-secret-alice; JSESSIONID=ajax:alice")
    async with session.service.client.governor.slot():
        pass
    await first.close()
    second = SessionManager(settings, SQLiteCounterStore(str(path)))
    try:
        with pytest.raises(SessionAuthError):
            await second.resolve(token)
        raw = path.read_bytes()
        assert token.encode() not in raw
        assert b"synthetic-secret-alice" not in raw
        assert b"ajax:alice" not in raw
    finally:
        await second.close()
