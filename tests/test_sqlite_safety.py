import asyncio
import os

import pytest

from fortross.linkedin.client import UpstreamGovernor
from fortross.linkedin.errors import CircuitOpenError, LinkedInSafetyError
from fortross.safety import SQLiteCounterStore, build_counter_store
from fortross.settings import Settings


async def test_file_reopen_preserves_counters_and_circuit(tmp_path):
    filename = tmp_path / ".state" / "safety.sqlite3"
    first = SQLiteCounterStore(str(filename))
    settings = Settings(linkedin_li_at="fake-session", linkedin_max_requests_per_hour=1)
    governor = UpstreamGovernor(settings, first)
    async with governor.slot():
        pass
    second = SQLiteCounterStore(str(filename))
    with pytest.raises(LinkedInSafetyError, match="budget exhausted"):
        async with UpstreamGovernor(settings, second).slot():
            pytest.fail("A new client must keep the budget")
    await governor.trip()
    with pytest.raises(CircuitOpenError):
        async with UpstreamGovernor(settings, second).slot():
            pytest.fail("A new client must keep the circuit")
    assert "fake-session" not in filename.read_bytes().decode(errors="ignore")
    if os.name == "posix":
        assert filename.stat().st_mode & 0o777 == 0o600


async def test_atomic_file_increments_and_block_extension(tmp_path):
    stores = [SQLiteCounterStore(str(tmp_path / "safety.sqlite3")) for _ in range(2)]
    results = await asyncio.gather(*(stores[i % 2].increment("key", 0) for i in range(20)))
    assert sorted(results) == list(range(1, 21))
    await stores[0].block_until("key", 100)
    await stores[1].block_until("key", 50)
    assert await stores[0].blocked_until("key") == 100


async def test_corrupt_file_fails_closed_without_replacement(tmp_path):
    path = tmp_path / "broken.sqlite3"
    path.write_bytes(b"invalid-database")
    with pytest.raises(LinkedInSafetyError):
        async with UpstreamGovernor(Settings(), SQLiteCounterStore(str(path))).slot():
            pytest.fail("Must not contact LinkedIn")
    assert path.read_bytes() == b"invalid-database"


def test_production_can_use_file_without_turso(tmp_path):
    settings = Settings(
        app_env="production",
        api_key="synthetic-long-api-key-32-characters",
        linkedin_li_at="fake",
        linkedin_jsessionid="ajax:fake",
        allowed_profile_slugs=("example",),
        safety_state_file=str(tmp_path / "state.sqlite3"),
    )
    settings.validate_live_configuration()
    assert isinstance(build_counter_store(settings), SQLiteCounterStore)
