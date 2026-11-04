"""The MCP surface.

Two levels:
* in-process — tools are registered and callable (`Client(mcp)`)
* over stdio   — a real subprocess speaking MCP, which is exactly how DSH launches it
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from doomscroller.mcp_server import mcp
from doomscroller.store import record_event
from doomscroller.subscriptions import subscribe

PROJECT_ROOT = Path(__file__).resolve().parent.parent

EXPECTED_TOOLS = {
    "list_picked_chats",
    "connector_status",
    "read_messages",
    "new_messages",
    "search_messages",
    "message_events",
    "archive_stats",
}


def _text(result) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


def _seed(session):
    sub = subscribe(session, "seedchat")
    sub.chat_id = -1005550001111
    sub.cursor_message_id = 3
    session.commit()
    for i in range(1, 6):
        record_event(
            session,
            chat_id=-1005550001111,
            message_id=i,
            event_type="new",
            text=f"hello {i}" + (" grant opportunity" if i == 4 else ""),
            sender_id=1,
            sender_name="alice",
        )
    record_event(session, chat_id=-1005550001111, message_id=2, event_type="edited", text="hello 2 (edited)")
    session.commit()


async def test_all_tools_are_registered():
    async with Client(mcp) as client:
        listed = {t.name for t in (await client.list_tools()).tools}
    assert listed == EXPECTED_TOOLS, f"unexpected tool set: {listed}"


async def test_list_picked_chats(session):
    _seed(session)
    async with Client(mcp) as client:
        payload = json.loads(_text(await client.call_tool("list_picked_chats", {})))
    assert [c["chat"] for c in payload] == ["seedchat"]
    assert payload[0]["cursor_message_id"] == 3
    assert payload[0]["chat_id"] == -1005550001111


async def test_read_messages_respects_limit_and_is_newest_first(session):
    _seed(session)
    async with Client(mcp) as client:
        payload = json.loads(
            _text(await client.call_tool("read_messages", {"chat": "seedchat", "limit": 2}))
        )
    assert payload["count"] == 2
    assert [m["message_id"] for m in payload["messages"]] == [5, 4]


async def test_read_messages_pagination(session):
    _seed(session)
    async with Client(mcp) as client:
        payload = json.loads(
            _text(await client.call_tool("read_messages", {"chat": "seedchat", "before_id": 3}))
        )
    assert [m["message_id"] for m in payload["messages"]] == [2, 1]


async def test_new_messages_uses_cursor(session):
    _seed(session)
    async with Client(mcp) as client:
        payload = json.loads(
            _text(await client.call_tool("new_messages", {"chat": "seedchat"}))
        )
    assert [m["message_id"] for m in payload["messages"]] == [4, 5]


async def test_search_messages(session):
    _seed(session)
    async with Client(mcp) as client:
        payload = json.loads(
            _text(await client.call_tool("search_messages", {"query": "grant"}))
        )
    assert payload["count"] == 1
    assert payload["messages"][0]["message_id"] == 4


async def test_message_events_shows_full_history(session):
    _seed(session)
    async with Client(mcp) as client:
        payload = json.loads(
            _text(await client.call_tool("message_events", {"chat": "seedchat", "message_id": 2}))
        )
    assert [e["event"] for e in payload["events"]] == ["new", "edited"]


async def test_unknown_chat_is_a_clear_error(session):
    _seed(session)
    async with Client(mcp) as client:
        result = await client.call_tool("read_messages", {"chat": "nope"})
    assert result.is_error or "not subscribed" in _text(result)


async def test_connector_status_reports_readiness(session):
    _seed(session)
    async with Client(mcp) as client:
        payload = json.loads(_text(await client.call_tool("connector_status", {})))
    assert payload["database"]["ok"] is True
    assert payload["subscriptions"]["total"] == 1
    assert payload["telegram"]["proxy"] in {"direct", "configured"}
    # the proxy line must never carry a secret
    assert "secret" not in json.dumps(payload["telegram"]).lower()


async def test_stdio_subprocess_speaks_mcp(session):
    """The real transport DSH uses: spawn the server, handshake, call a tool."""
    _seed(session)
    env = {
        **os.environ,
        "DATABASE_URL": os.environ["DATABASE_URL"],
        "PYTHONPATH": str(PROJECT_ROOT),
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "doomscroller", "mcp", "--transport", "stdio"],
        env=env,
        cwd=str(PROJECT_ROOT),
    )
    async with Client(params) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        assert tools == EXPECTED_TOOLS
        payload = json.loads(
            _text(await client.call_tool("list_picked_chats", {}))
        )
    assert payload[0]["chat"] == "seedchat"
