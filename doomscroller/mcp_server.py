"""MCP interface — read-only tools over the archived messages.

Deliberately read-only: no send tool, no ingest-triggering tool. Ingest belongs to the
service that owns the Telegram session, so the MCP process never touches the session file
and never contends for it.

Tool names are identical across every client (DSH, Claude Code, Cursor): a stdio server
like this one is understood by all of them.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from mcp.server import MCPServer
from sqlmodel import Session, select

from . import __version__
from .db import healthcheck, session_scope
from .models import Message, Subscription
from .store import (
    events_for_message,
    fetch_messages,
    new_messages as store_new_messages,
    search_messages as store_search,
    stats as store_stats,
)
from .subscriptions import get_subscription, list_subscriptions, normalise_ref

logger = logging.getLogger(__name__)

_db_ready = False


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _ensure_db() -> None:
    global _db_ready
    if not _db_ready:
        from .db import init_db

        init_db()
        _db_ready = True


def _chat_id(session: Session, chat: str) -> int:
    sub = get_subscription(session, chat)
    if sub is not None and sub.chat_id is not None:
        return int(sub.chat_id)
    norm = normalise_ref(chat)
    if norm.lstrip("-").isdigit():
        return int(norm)
    if sub is not None:
        raise ValueError(
            f"{chat!r} is subscribed but its Telegram id is not resolved yet — "
            "the ingest service must poll it once before messages can be read."
        )
    raise ValueError(
        f"{chat!r} is not subscribed. Known chats: "
        f"{[s.chat_ref for s in list_subscriptions(session)] or 'none'}"
    )


def _message(m: Message) -> dict:
    return {
        "chat_id": m.chat_id,
        "message_id": m.message_id,
        "date": m.date.isoformat() if m.date else None,
        "sender": m.sender_name,
        "sender_id": m.sender_id,
        "text": m.text,
        "reply_to": m.reply_to_message_id,
        "edited": m.edited_at.isoformat() if m.edited_at else None,
        "deleted": m.deleted,
    }


mcp = MCPServer(
    name="doomscroller",
    version=__version__,
    instructions=(
        "Read access to an archive of Telegram channels/chats stored in PostgreSQL. "
        "Use list_picked_chats first to discover what is archived, then read_messages or "
        "search_messages. All tools are read-only."
    ),
)


@mcp.tool()
def list_picked_chats() -> str:
    """List every subscribed channel/chat, with its cursor, message count and last error."""
    _ensure_db()
    with session_scope() as session:
        subs = list_subscriptions(session)
        return _json(
            [
                {
                    "chat": s.chat_ref,
                    "chat_id": s.chat_id,
                    "title": s.title,
                    "kind": s.kind,
                    "enabled": s.enabled,
                    "cursor_message_id": s.cursor_message_id,
                    "message_count": s.message_count,
                    "last_polled_at": s.last_polled_at.isoformat() if s.last_polled_at else None,
                    "last_error": s.last_error,
                }
                for s in subs
            ]
        )


@mcp.tool()
def connector_status() -> str:
    """Readiness check: is the database reachable, is Telegram configured, is anything stale?

    Does not connect to Telegram — it reports on configuration and stored state, so it is
    safe and instant to call.
    """
    report: dict[str, Any] = {"tool_version": __version__}

    try:
        report["database"] = healthcheck()
    except Exception as exc:  # noqa: BLE001
        report["database"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return _json(report)

    data_dir = Path(os.environ.get("DATA_DIR") or ".").expanduser()
    session_name = os.environ.get("TELEGRAM_SESSION", "scroller_session")
    report["telegram"] = {
        "api_id_set": bool(os.environ.get("TELEGRAM_API_ID")),
        "api_hash_set": bool(os.environ.get("TELEGRAM_API_HASH")),
        "session_file": str(data_dir / f"{session_name}.session"),
        "session_present": (data_dir / f"{session_name}.session").exists(),
        "proxy": "configured" if os.environ.get("TG_PROXY", "").strip() not in ("", "off") else "direct",
    }

    _ensure_db()
    with session_scope() as session:
        subs = list_subscriptions(session)
        now = datetime.now(timezone.utc)
        stale = []
        for s in subs:
            age = (
                (now - s.last_polled_at).total_seconds()
                if s.last_polled_at and s.last_polled_at.tzinfo
                else None
            )
            if s.last_error or age is None or age > 3600:
                stale.append(
                    {
                        "chat": s.chat_ref,
                        "seconds_since_poll": int(age) if age is not None else None,
                        "last_error": s.last_error,
                    }
                )
        report["subscriptions"] = {
            "total": len(subs),
            "enabled": sum(1 for s in subs if s.enabled),
            "stale_or_failing": stale,
        }
        report["counts"] = store_stats(session)
    return _json(report)


@mcp.tool()
def read_messages(chat: str, limit: int = 50, before_id: Optional[int] = None, after_id: Optional[int] = None) -> str:
    """Read stored messages from a chat, newest first.

    Args:
        chat: subscription reference, e.g. "demo_channel" or "t.me/demo_channel".
        limit: maximum rows to return (1-1000).
        before_id: page backwards — only messages older than this Telegram message id.
        after_id: page forwards — only messages newer than this Telegram message id.
    """
    _ensure_db()
    limit = max(1, min(int(limit), 1000))
    with session_scope() as session:
        chat_id = _chat_id(session, chat)
        rows = fetch_messages(
            session, chat_id, limit=limit, before_id=before_id, after_id=after_id
        )
        return _json({"chat": chat, "chat_id": chat_id, "count": len(rows),
                      "messages": [_message(m) for m in rows]})


@mcp.tool()
def new_messages(chat: str, since_id: Optional[int] = None) -> str:
    """Everything newer than the stored cursor (or than `since_id` if given).

    This is "what arrived since last time" — it does not advance the cursor.
    """
    _ensure_db()
    with session_scope() as session:
        chat_id = _chat_id(session, chat)
        rows = store_new_messages(session, chat_id, since_id=since_id)
        return _json({"chat": chat, "chat_id": chat_id, "count": len(rows),
                      "messages": [_message(m) for m in rows]})


@mcp.tool()
def search_messages(query: str, chat: Optional[str] = None, limit: int = 20) -> str:
    """Case-insensitive substring search over archived messages, optionally scoped to one chat."""
    _ensure_db()
    limit = max(1, min(int(limit), 200))
    with session_scope() as session:
        chat_id = _chat_id(session, chat) if chat else None
        rows = store_search(session, query, chat_id=chat_id, limit=limit)
        return _json({"query": query, "scope": chat or "all chats",
                      "count": len(rows), "messages": [_message(m) for m in rows]})


@mcp.tool()
def message_events(chat: str, message_id: int) -> str:
    """The append-only event log for one message: every new/edit/delete, in order."""
    _ensure_db()
    with session_scope() as session:
        chat_id = _chat_id(session, chat)
        events = events_for_message(session, chat_id, int(message_id))
        return _json({
            "chat": chat,
            "message_id": message_id,
            "count": len(events),
            "events": [
                {"event": e.event_type, "at": e.created_at.isoformat(), "text": e.text}
                for e in events
            ],
        })


@mcp.tool()
def archive_stats() -> str:
    """Row counts per table (subscriptions, messages, events, deleted)."""
    _ensure_db()
    with session_scope() as session:
        return _json(store_stats(session))


def run(transport: str = "stdio", **kwargs: Any) -> None:
    """Start the MCP server. `transport` is 'stdio' or 'streamable-http'."""
    mcp.run(transport=transport, **kwargs)


if __name__ == "__main__":  # pragma: no cover
    run()
