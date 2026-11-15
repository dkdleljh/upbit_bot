"""The complete test suite is offline and cannot use developer credentials."""
import socket
import pytest


@pytest.fixture(autouse=True)
def offline_test_environment(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Network access forbidden in tests")
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket, 'create_connection', deny)
    monkeypatch.setattr(socket, 'getaddrinfo', deny)
    for name in ('UPBIT_ACCESS_KEY', 'UPBIT_SECRET_KEY', 'UPBIT_LIVE_CONFIRM',
                 'UPBIT_KILL_SWITCH', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'):
        monkeypatch.delenv(name, raising=False)
