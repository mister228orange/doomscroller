"""Webhook forwarding: subscribe a chat, deliver arriving messages, unsubscribe.

Two levels, like the MCP tests: the tools over an in-process MCP client, and the delivery
path itself (payload shape, the token header, failure bookkeeping).
"""

from __future__ import annotations

import json

import pytest
from mcp import Client

from doomscroller import webhooks
from doomscroller.mcp_server import mcp
from doomscroller.models import ChatWebhook
from doomscroller.store import get_webhook, list_webhooks, record_delivery, upsert_webhook
from doomscroller.subscriptions import subscribe


def _text(result) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


@pytest.fixture
def seeded(session):
    sub = subscribe(session, "hookchat")
    sub.chat_id = -1009990001111
    session.commit()
    return -1009990001111


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("WEBHOOK_TOKEN", "s3cret")
    yield


# ── the tools ────────────────────────────────────────────────────────────────

async def test_subscribe_stores_the_webhook(session, seeded):
    async with Client(mcp) as client:
        payload = json.loads(_text(await client.call_tool(
            "webhook_subscribe", {"chat": "hookchat", "url": "https://example.test/hook"})))
    assert payload["ok"] is True and payload["subscribed"] is True
    assert payload["chat_id"] == seeded
    assert payload["url"] == "https://example.test/hook"
    row = get_webhook(session, seeded)
    assert row is not None and row.url == "https://example.test/hook"


async def test_subscribe_twice_replaces_the_url(session, seeded):
    async with Client(mcp) as client:
        await client.call_tool("webhook_subscribe", {"chat": "hookchat", "url": "http://a.test/x"})
        await client.call_tool("webhook_subscribe", {"chat": "hookchat", "url": "http://b.test/y"})
    rows = list_webhooks(session)
    assert len(rows) == 1
    assert rows[0].url == "http://b.test/y"


async def test_unsubscribe_removes_it(session, seeded):
    async with Client(mcp) as client:
        await client.call_tool("webhook_subscribe", {"chat": "hookchat", "url": "http://a.test/x"})
        payload = json.loads(_text(await client.call_tool(
            "webhook_unsubscribe", {"chat": "hookchat"})))
    assert payload["subscribed"] is False and payload["was_subscribed"] is True
    assert list_webhooks(session) == []


async def test_unsubscribe_when_not_subscribed_is_reported(session, seeded):
    async with Client(mcp) as client:
        payload = json.loads(_text(await client.call_tool(
            "webhook_unsubscribe", {"chat": "hookchat"})))
    assert payload["ok"] is True
    assert payload["was_subscribed"] is False


async def test_subscribe_is_refused_without_a_token(session, seeded, monkeypatch):
    monkeypatch.delenv("WEBHOOK_TOKEN", raising=False)
    async with Client(mcp) as client:
        payload = json.loads(_text(await client.call_tool(
            "webhook_subscribe", {"chat": "hookchat", "url": "http://a.test/x"})))
    assert payload["refused"] is True
    assert "WEBHOOK_TOKEN" in payload["reason"]
    assert list_webhooks(session) == []


async def test_non_http_urls_are_rejected(session, seeded):
    async with Client(mcp) as client:
        payload = json.loads(_text(await client.call_tool(
            "webhook_subscribe", {"chat": "hookchat", "url": "file:///etc/passwd"})))
    assert payload["subscribed"] is False
    assert "http(s)" in payload["reason"]


async def test_list_reports_counters(session, seeded):
    upsert_webhook(session, seeded, "http://a.test/x", chat_ref="hookchat")
    record_delivery(session, seeded)
    record_delivery(session, seeded, error="HTTP 500: boom")
    session.commit()
    async with Client(mcp) as client:
        payload = json.loads(_text(await client.call_tool("webhook_list", {})))
    assert len(payload) == 1
    assert payload[0]["delivered"] == 1
    assert payload[0]["failed"] == 1
    assert "boom" in payload[0]["last_error"]


# ── delivery ─────────────────────────────────────────────────────────────────

class _Response:
    def __init__(self, status: int, text: str = "") -> None:
        self.status_code = status
        self.text = text


class _Recorder:
    def __init__(self, status: int = 200, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.status = status
        self.error = error

    async def post(self, url, json=None, headers=None, timeout=None):  # noqa: A002
        if self.error:
            raise self.error
        self.calls.append({"url": url, "body": json, "headers": headers})
        return _Response(self.status, "nope")


async def test_deliver_posts_json_with_the_token_header():
    rec = _Recorder()
    error = await webhooks.deliver("http://a.test/x", {"k": 1}, client=rec)
    assert error is None
    assert rec.calls[0]["url"] == "http://a.test/x"
    assert rec.calls[0]["body"] == {"k": 1}
    assert rec.calls[0]["headers"]["X-Webhook-Token"] == "s3cret"


async def test_deliver_reports_http_errors():
    rec = _Recorder(status=503)
    error = await webhooks.deliver("http://a.test/x", {}, client=rec)
    assert error and "503" in error


async def test_deliver_reports_network_errors():
    rec = _Recorder(error=OSError("connection refused"))
    error = await webhooks.deliver("http://a.test/x", {}, client=rec)
    assert error and "refused" in error


async def test_forward_to_chat_posts_and_counts(session, seeded, monkeypatch):
    upsert_webhook(session, seeded, "http://a.test/x", chat_ref="hookchat")
    session.commit()
    rec = _Recorder()
    real_deliver = webhooks.deliver

    async def fake_deliver(url, payload, **kwargs):
        return await real_deliver(url, payload, client=rec)

    monkeypatch.setattr(webhooks, "deliver", fake_deliver)
    error = await webhooks.forward_to_chat(seeded, {"message_id": 7, "raw": {"id": 7}})
    assert error is None
    body = rec.calls[0]["body"]
    assert body["message_id"] == 7
    assert body["chat_id"] == seeded
    assert body["raw"] == {"id": 7}
    assert get_webhook(session, seeded).delivered == 1


async def test_forward_to_chat_is_a_noop_without_a_webhook(session, seeded):
    rec = _Recorder()
    assert await webhooks.forward_to_chat(seeded, {"message_id": 1}) is None
    assert rec.calls == []


async def test_unsubscribe_stops_delivery_immediately(session, seeded, monkeypatch):
    upsert_webhook(session, seeded, "http://a.test/x", chat_ref="hookchat")
    session.commit()
    rec = _Recorder()
    real_deliver = webhooks.deliver

    async def fake_deliver(url, payload, **kwargs):
        return await real_deliver(url, payload, client=rec)

    monkeypatch.setattr(webhooks, "deliver", fake_deliver)
    await webhooks.forward_to_chat(seeded, {"message_id": 1})
    session.delete(get_webhook(session, seeded))
    session.commit()
    await webhooks.forward_to_chat(seeded, {"message_id": 2})
    assert len(rec.calls) == 1


async def test_a_failed_delivery_is_recorded_not_raised(session, seeded, monkeypatch):
    upsert_webhook(session, seeded, "http://a.test/x", chat_ref="hookchat")
    session.commit()

    async def fake_deliver(url, payload, **kwargs):
        return "HTTP 500: boom"

    monkeypatch.setattr(webhooks, "deliver", fake_deliver)
    error = await webhooks.forward_to_chat(seeded, {"message_id": 1})
    assert error == "HTTP 500: boom"
    row = get_webhook(session, seeded)
    assert row.failed == 1
    assert "boom" in row.last_error


# ── payload shape ────────────────────────────────────────────────────────────

class _FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id

    def to_dict(self) -> dict:
        return {"id": self.id, "message": "hello", "from_id": {"user_id": 1}}


class _FakeEvent:
    chat_id = -1009990001111

    def __init__(self, message_id: int = 5) -> None:
        self.message = _FakeMessage(message_id)


def test_update_payload_keeps_the_raw_update():
    payload = webhooks.update_payload(_FakeEvent(5))
    assert payload["chat_id"] == -1009990001111
    assert payload["message_id"] == 5
    assert payload["raw"]["id"] == 5
    assert payload["raw"]["from_id"] == {"user_id": 1}
    json.dumps(payload)  # must be JSON-serialisable


def test_register_forwarder_attaches_a_handler():
    class FakeClient:
        def __init__(self) -> None:
            self.handlers = []

        def on(self, event):
            def decorator(func):
                self.handlers.append(func)
                return func
            return decorator

    client = FakeClient()
    webhooks.register_forwarder(client)
    assert len(client.handlers) == 1
