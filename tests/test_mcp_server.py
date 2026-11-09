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
from doomscroller.models import PostScore
from doomscroller.store import record_event
from doomscroller.subscriptions import subscribe

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ORIGINAL_DB_URL = os.environ.get("DATABASE_URL", "")

EXPECTED_TOOLS = {
    "list_picked_chats",
    "connector_status",
    "read_messages",
    "new_messages",
    "search_messages",
    "message_events",
    "chat_history",
    "archive_stats",
    "list_chats",
    "send_message",
    "post_scores",
    "channel_states",
    "channel_events",
    "webhook_subscribe",
    "webhook_unsubscribe",
    "webhook_list",
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


async def test_chat_history_is_one_flat_compressed_array(session):
    _seed(session)
    async with Client(mcp) as client:
        payload = json.loads(_text(await client.call_tool("chat_history", {"chat": "seedchat"})))
    assert isinstance(payload, list)
    # oldest first, every stored message, and a lean shape — nothing but the essentials
    assert [m["id"] for m in payload] == [1, 2, 3, 4, 5]
    assert set(payload[0]) <= {"id", "date", "from", "t", "more"}
    assert payload[0]["from"] == "alice"
    assert payload[1]["t"] == "hello 2 (edited)"  # edits land in the current text
    assert all("reply_to" not in m and "deleted" not in m for m in payload)


async def test_chat_history_truncates_and_strips_noise(session):
    record_event(
        session,
        chat_id=-1005550002222,
        message_id=1,
        event_type="new",
        text="see https://example.com/a/very/long/link and buy  now",
        sender_name="bob",
    )
    session.commit()
    sub = subscribe(session, "noisechat")
    sub.chat_id = -1005550002222
    session.commit()
    async with Client(mcp) as client:
        payload = json.loads(
            _text(await client.call_tool("chat_history", {"chat": "noisechat", "limit": 10}))
        )
    item = payload[0]
    assert "example.com" not in item["t"]  # URLs are dropped
    assert "  " not in item["t"]  # whitespace collapsed
    assert item["t"] == "see and buy now"[:10]
    assert item["more"] is True


async def test_chat_history_limit_zero_keeps_no_text(session):
    _seed(session)
    async with Client(mcp) as client:
        payload = json.loads(
            _text(await client.call_tool("chat_history", {"chat": "seedchat", "limit": 0}))
        )
    assert [m["t"] for m in payload] == [""] * 5
    assert payload[0]["more"] is True  # text existed, it was just cut away


async def test_chat_history_after_id(session):
    _seed(session)
    async with Client(mcp) as client:
        payload = json.loads(
            _text(await client.call_tool("chat_history", {"chat": "seedchat", "after_id": 3}))
        )
    assert [m["id"] for m in payload] == [4, 5]


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


# ── scoring surface ──────────────────────────────────────────────────────────

def _seed_scores(session):
    from doomscroller.scoring.state import apply_score, get_or_create_state

    state = get_or_create_state(session, chat_id=-1005550001111, chat_ref="seedchat")
    for index, (kpd_i, primary) in enumerate([(8.0, "SCI"), (4.0, "TECH")], start=1):
        row = PostScore(
            chat_id=-1005550001111,
            message_id=index,
            status="scored",
            primary=primary,
            secondary=["TRUST"],
            main_thesis=f"тезис {index}",
            score_i=kpd_i,
            score_v=kpd_i,
            score_u=kpd_i,
            score_p=5.0,
            score_s=5.0,
            score_d=kpd_i,
            kpd=kpd_i,
            eff=kpd_i / 5.0,
            confidence="medium",
        )
        apply_score(session, state, row)
    session.commit()


async def test_post_scores_tool_returns_the_rubric_rows(session):
    _seed(session)
    _seed_scores(session)
    async with Client(mcp) as client:
        payload = json.loads(_text(await client.call_tool("post_scores", {"chat": "seedchat"})))
    assert payload["count"] == 2
    newest = payload["scores"][0]
    assert newest["message_id"] == 2
    assert newest["scores"]["I"] == 4.0
    assert newest["kpd"] == 4.0
    assert newest["deviation"] is not None
    assert newest["trend_position"] in {"above", "at", "below"}


async def test_channel_states_tool_returns_the_trajectory(session):
    _seed(session)
    _seed_scores(session)
    async with Client(mcp) as client:
        payload = json.loads(_text(await client.call_tool("channel_states", {})))
    assert len(payload) == 1
    state = payload[0]
    assert state["total_scored"] == 2
    assert state["means"]["I"] == 7.2  # (8*4 + 4) / 5
    assert state["tag_distribution"] == {"SCI": 1, "TECH": 1}
    assert len(state["local_anchors"]) == 2


async def test_channel_events_tool_returns_the_append_only_log(session):
    _seed(session)
    _seed_scores(session)
    async with Client(mcp) as client:
        payload = json.loads(_text(await client.call_tool("channel_events", {"chat": "seedchat"})))
    # the first post set a new channel best, which apply_score logged as a peak
    assert payload["count"] == 1
    assert payload["events"][0]["kind"] == "peak"
    assert payload["events"][0]["message_id"] == 1


async def test_connector_status_reports_scoring_readiness(session):
    _seed(session)
    _seed_scores(session)
    async with Client(mcp) as client:
        payload = json.loads(_text(await client.call_tool("connector_status", {})))
    assert payload["scoring"]["scored_posts"] == 2
    assert payload["scoring"]["channels"] == 1


# ── connector_status must answer, never hang ────────────────────────────────
#
# The client gives a tool call ~30 s and then reports a bare "Request timed out". A hanging
# database used to burn exactly that budget: there was no connect timeout, so the report
# never came back and the agent had no idea what was wrong.

def _reset_engine(url: str) -> None:
    from doomscroller import db as db_module

    db_module._engine = None
    os.environ["DATABASE_URL"] = url


async def test_connector_status_returns_a_report_when_the_database_is_down():
    _reset_engine("postgresql+psycopg://doomscroller:doomscroller@127.0.0.1:5999/doomscroller")
    try:
        async with Client(mcp) as client:
            payload = json.loads(_text(await client.call_tool("connector_status", {})))
    finally:
        _reset_engine(os.environ.get("TEST_DATABASE_URL") or _ORIGINAL_DB_URL)
    assert payload["database"]["ok"] is False
    assert payload["database"]["error"]
    assert isinstance(payload["database"].get("hint"), str)
    # configuration is reported even when the database is unreachable
    assert "telegram" in payload


async def test_db_connect_args_bound_the_connect_timeout():
    from doomscroller.db import _connect_args

    assert _connect_args("postgresql+psycopg://u:p@h:5432/db") == {"connect_timeout": 5}
    assert _connect_args("sqlite:///tmp/x.sqlite") == {}
    # an explicit query parameter wins over the default
    assert _connect_args("postgresql+psycopg://u:p@h:5432/db?connect_timeout=2") == {}

    os.environ["DB_CONNECT_TIMEOUT"] = "1"
    try:
        assert _connect_args("postgresql+psycopg://u:p@h:5432/db") == {"connect_timeout": 1}
    finally:
        os.environ.pop("DB_CONNECT_TIMEOUT", None)


async def test_connector_status_reports_a_query_failure(session, monkeypatch):
    """A reachable DB whose tables/query blow up must still produce a report."""
    _seed(session)
    import doomscroller.mcp_server as server

    def boom(_session):
        raise RuntimeError("relation \"messages\" does not exist")

    monkeypatch.setattr(server, "store_stats", boom)
    async with Client(mcp) as client:
        payload = json.loads(_text(await client.call_tool("connector_status", {})))
    assert payload["database"]["ok"] is False
    assert "does not exist" in payload["database"]["error"]
    assert "hint" in payload["database"]
    assert "telegram" in payload
