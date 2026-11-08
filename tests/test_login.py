"""The resumable terminal login: revoked-session heal, code request, sign-in."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

from telethon.errors import (
    AuthKeyDuplicatedError,
    SessionPasswordNeededError,
)

from doomscroller.telegram_client import login_flow

PHONE = "+10000000000"


class FakeClient:
    """Enough of Telethon for the flow: connect may fail once with a revoked key."""

    def __init__(self, tmp: Path, *, authorized: bool, fail_first: bool = False):
        self.session = SimpleNamespace(filename=str(tmp / "scroller_session.session"))
        (tmp / "scroller_session.session").write_bytes(b"dead")
        self._authorized = authorized
        self._fail_first = fail_first
        self.connect_calls = 0
        self.sign_in_kwargs: dict | None = None

    def is_connected(self) -> bool:
        return False

    async def connect(self):
        self.connect_calls += 1
        if self._fail_first and self.connect_calls == 1:
            raise AuthKeyDuplicatedError(request=None)

    async def is_user_authorized(self) -> bool:
        return self._authorized

    async def get_me(self):
        return SimpleNamespace(username="tester")

    async def send_code_request(self, phone: str):
        assert phone == PHONE
        return SimpleNamespace(phone_code_hash="hash-123")

    async def sign_in(self, **kwargs):
        self.sign_in_kwargs = kwargs


def _settings():
    return SimpleNamespace(phone=PHONE)


def test_a_revoked_session_is_moved_aside_and_login_continues(tmp_path):
    client = FakeClient(tmp_path, authorized=True, fail_first=True)
    result = asyncio.run(login_flow(client, _settings()))
    assert result["status"] == "already authorized"
    assert client.connect_calls == 2
    assert [p.name for p in tmp_path.iterdir() if "revoked-" in p.name]


def test_phase_one_requests_the_code_and_returns_the_hash(tmp_path):
    client = FakeClient(tmp_path, authorized=False)
    result = asyncio.run(login_flow(client, _settings()))
    assert result["status"] == "code sent"
    assert result["phone_code_hash"] == "hash-123"
    assert "--code" in result["next"]


def test_phase_two_signs_in_with_the_code_and_hash(tmp_path):
    client = FakeClient(tmp_path, authorized=False)
    result = asyncio.run(
        login_flow(client, _settings(), code="54321", phone_code_hash="hash-123")
    )
    assert result["status"] == "logged in"
    assert client.sign_in_kwargs == {
        "phone": PHONE,
        "code": "54321",
        "phone_code_hash": "hash-123",
    }


def test_two_factor_asks_for_the_password(tmp_path, monkeypatch):
    client = FakeClient(tmp_path, authorized=False)

    async def sign_in(**kwargs):
        client.sign_in_kwargs = kwargs
        if "password" not in kwargs:
            raise SessionPasswordNeededError(request=None)

    client.sign_in = sign_in
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "hunter2")
    result = asyncio.run(
        login_flow(client, _settings(), code="54321", phone_code_hash="hash-123")
    )
    assert result["status"] == "logged in"
    assert client.sign_in_kwargs == {"password": "hunter2"}


def test_a_tty_terminal_is_asked_for_the_code_in_the_same_process(tmp_path, monkeypatch):
    """One command for a human: the code prompt happens inside the same run."""
    import sys

    client = FakeClient(tmp_path, authorized=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "  54321 ")
    result = asyncio.run(login_flow(client, _settings()))
    assert result["status"] == "logged in"
    assert client.sign_in_kwargs == {
        "phone": PHONE,
        "code": "54321",
        "phone_code_hash": "hash-123",
    }
