import socket

import pytest

from fortross.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def isolate_tests(monkeypatch):
    """Tests cannot inherit real cookies/live mode or open network connections."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for key in Settings.model_fields:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key.upper(), raising=False)
    get_settings.cache_clear()

    def blocked(*args, **kwargs):
        raise AssertionError("Network access is forbidden in tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    yield
    get_settings.cache_clear()
