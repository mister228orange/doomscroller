"""The send guard — the security-critical part of the outbound path.

Sending acts as a real user account, so these rules must hold:
  * no allowlist            -> nothing can be sent
  * allowlist mismatch      -> refused
  * allowlist match         -> allowed (including @ / t.me spellings)
  * "*"                     -> allowed (deliberate opt-in)
  * rate limited            -> refused after the per-minute cap
"""

import pytest

from doomscroller import telegram_ops
from doomscroller.config import Settings
from doomscroller.telegram_ops import SendNotAllowed, assert_send_allowed, _check_rate


def _settings(allowlist=None, rate=20) -> Settings:
    return Settings(api_id=1, api_hash="x", send_allowlist=allowlist or [], send_rate_per_min=rate)


def test_no_allowlist_denies_everything():
    with pytest.raises(SendNotAllowed, match="disabled"):
        assert_send_allowed(_settings(), "anychat")


def test_allowlist_mismatch_is_refused():
    with pytest.raises(SendNotAllowed, match="not allowed"):
        assert_send_allowed(_settings(["safe_chat"]), "other_chat")


@pytest.mark.parametrize("spelling", ["safe_chat", "@safe_chat", "t.me/safe_chat",
                                      "https://t.me/safe_chat", "SAFE_CHAT"])
def test_allowlist_matches_equivalent_spellings(spelling):
    assert_send_allowed(_settings(["safe_chat"]), spelling)  # must not raise


def test_wildcard_allows_all():
    assert_send_allowed(_settings(["*"]), "anything")


def test_rate_limit_blocks_after_cap(monkeypatch):
    telegram_ops._send_times.clear()
    settings = _settings(["*"], rate=3)
    for _ in range(3):
        _check_rate(settings)          # consumes a slot each time
        telegram_ops._send_times.append(telegram_ops.time.time())
    with pytest.raises(SendNotAllowed, match="rate limit"):
        _check_rate(settings)
    telegram_ops._send_times.clear()


def test_empty_message_is_rejected(monkeypatch):
    import asyncio

    async def go():
        with pytest.raises(ValueError, match="empty"):
            await telegram_ops.send_text(None, _settings(["*"]), "chat", "   ")

    asyncio.run(go())


def test_send_denied_before_touching_telegram(monkeypatch):
    """The guard must fire before any network call is attempted."""
    import asyncio

    called = {"n": 0}

    async def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("should not reach Telegram")

    monkeypatch.setattr(telegram_ops, "resolve_chat", boom)

    async def go():
        with pytest.raises(SendNotAllowed):
            await telegram_ops.send_text(None, _settings([]), "chat", "hello")

    asyncio.run(go())
    assert called["n"] == 0, "guard let the call through to Telegram"
