"""MCP interface — the archive, the scores, and (opt-in) live Telegram.

The server is normally used from *outside* compose: a client such as DSH, Claude Code or
Cursor starts it over stdio on whatever machine the agent runs on. Three families of
tools, with different requirements:

* **Archive read (PostgreSQL only)** — ``read_messages``, ``new_messages``,
  ``search_messages``, ``message_events``, ``chat_history``, ``list_picked_chats``,
  ``archive_stats``.
  They need ``DATABASE_URL`` and nothing else: no Telegram credentials, no session, no
  model, no access to this repo.
* **Scoring read (PostgreSQL only)** — ``post_scores``, ``channel_states``,
  ``channel_events``, and the scoring block of ``connector_status``. The scorer service
  writes those rows; this process only reads them, and reports the models/prompt versions
  actually recorded in the database rather than its own environment.
* **Live Telegram (needs credentials + its own session)** — ``list_chats`` and
  ``send_message``. The client is opened lazily on first use, with a separate session file
  (``TELEGRAM_MCP_SESSION``, default ``<session>_mcp``) so this process never contends with
  the poller for the same SQLite session. Sending is denied by default and requires
  ``SEND_ALLOWLIST``; it acts as the real user account, not a bot.

Ingest is deliberately not exposed: storing messages belongs to the service that owns the
poller's session, and the cursor is committed there.

Tool names are identical across every client (DSH, Claude Code, Cursor): a stdio server
like this one is understood by all of them.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from mcp.server import MCPServer
from sqlmodel import Session, select

from . import __version__
from .db import healthcheck, session_scope
from .models import ChannelEvent, ChannelState, Message, PostScore, Subscription
from .store import (
    all_messages,
    delete_webhook,
    events_for_message,
    fetch_messages,
    list_webhooks,
    new_messages as store_new_messages,
    search_messages as store_search,
    stats as store_stats,
    upsert_webhook,
)
from .scoring.state import pending_counts, state_snapshot
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
        "Read access to an archive of Telegram channels/chats stored in PostgreSQL, plus "
        "live Telegram chat listing and (allowlisted) sending. Use list_picked_chats for "
        "the archive, list_chats for live chats, then read_messages or search_messages. "
        "send_message acts as the real user account and is refused unless the target chat "
        "is listed in SEND_ALLOWLIST."
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

    Always returns a report, even when the database is down: the connection is bounded by
    ``DB_CONNECT_TIMEOUT`` (default 5 s) and every extra query has its own budget, because
    an MCP client kills the call at ~30 s with an uninformative "Request timed out".
    """
    report: dict[str, Any] = {"tool_version": __version__}

    data_dir = Path(os.environ.get("DATA_DIR") or ".").expanduser()
    session_name = os.environ.get("TELEGRAM_SESSION", "scroller_session")
    # Configuration is always reportable — a readiness check whose database is down should
    # still say whether the rest of the setup looks right.
    report["telegram"] = {
        "api_id_set": bool(os.environ.get("TELEGRAM_API_ID")),
        "api_hash_set": bool(os.environ.get("TELEGRAM_API_HASH")),
        "session_file": str(data_dir / f"{session_name}.session"),
        "session_present": (data_dir / f"{session_name}.session").exists(),
        "proxy": "configured" if os.environ.get("TG_PROXY", "").strip() not in ("", "off") else "direct",
    }

    try:
        report["database"] = healthcheck()
    except Exception as exc:  # noqa: BLE001
        report["database"] = _db_failure(exc)
        return _json(report)

    try:
        report["subscriptions"], report["counts"], report["scoring"] = _status_snapshot()
    except Exception as exc:  # noqa: BLE001
        # The database answered the probe but a query failed (missing tables, a lock, a
        # timeout): report that instead of dragging the whole call past the client's limit.
        report["database"] = _db_failure(exc)
        return _json(report)
    return _json(report)


def _db_failure(exc: BaseException) -> dict:
    """A structured ``database`` block for a failed connection or query."""
    detail = str(exc)
    hint = None
    if "ConnectionTimeout" in detail or "connection timeout expired" in detail:
        hint = (
            "PostgreSQL accepted the TCP connection but never completed the startup "
            "handshake. Check the container/port mapping (compose maps db to "
            "127.0.0.1:5434) and that the host can actually reach it."
        )
    elif "Connection refused" in detail:
        hint = "Nothing is listening on that host:port — start the database (docker compose up -d db)."
    elif "does not exist" in detail and "relation" in detail:
        hint = "The database is reachable but empty — run the ingest/scorer once to create tables."
    block: dict[str, Any] = {"ok": False, "error": f"{type(exc).__name__}: {detail}"}
    if hint:
        block["hint"] = hint
    return block


def _status_snapshot() -> tuple[dict, dict, dict]:
    """Subscriptions, row counts and scoring facts — read-only, one session."""
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
        subscriptions = {
            "total": len(subs),
            "enabled": sum(1 for s in subs if s.enabled),
            "stale_or_failing": stale,
        }
        counts = store_stats(session)
        # Scoring facts come from the database, never from this process's environment: the
        # MCP server is not the scorer and usually runs on a different machine, where
        # SCORER_* is not set (and where the only thing that must be configured is the DB).
        scored = session.exec(select(PostScore).where(PostScore.status == "scored")).all()
        channels = session.exec(select(ChannelState)).all()
        stamps = [row.scored_at for row in scored if row.scored_at is not None]
        scoring = {
            "channels": len(channels),
            "scored_posts": len(scored),
            "skipped_posts": len(
                session.exec(select(PostScore).where(PostScore.status == "skipped")).all()
            ),
            "failed_posts": len(
                session.exec(select(PostScore).where(PostScore.status == "failed")).all()
            ),
            "pending_posts": sum(pending_counts(session).values()),
            "models_in_use": sorted({row.model for row in scored if row.model}),
            "prompt_versions": sorted(
                {row.prompt_version for row in scored if row.prompt_version}
            ),
            "last_scored_at": max(stamps).isoformat() if stamps else None,
        }
    return subscriptions, counts, scoring


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


# ── compressed history ────────────────────────────────────────────────────────

# How much of a message counts as "the gist" before it is cut with an ellipsis. A whole
# channel history is long; the point of the compressed view is that it fits in one context.
COMPRESS_CHARS_DEFAULT = 280
_URL_RE = re.compile(r"https?://\S+")
_SPACE_RE = re.compile(r"\s+")


def _gist(text: Optional[str], limit: int) -> dict[str, Any]:
    """One message → its first ``limit`` characters, with the obvious noise removed.

    Dropped: URLs (they are rarely the point and eat the whole budget), zero-width and
    other control characters, and runs of whitespace. Line breaks are kept as a single
    space because one row is one message in the output array.
    """
    raw = text or ""
    body = _URL_RE.sub(" ", raw)
    body = "".join(ch for ch in body if ch.isprintable() or ch in "\n\t")
    body = _SPACE_RE.sub(" ", body).strip()
    out: dict[str, Any] = {"t": body[:limit]}
    if len(body) > limit:
        out["more"] = True
    return out


@mcp.tool()
def chat_history(
    chat: str,
    limit: int = COMPRESS_CHARS_DEFAULT,
    after_id: Optional[int] = None,
    include_deleted: bool = False,
) -> str:
    """The whole history of one chat, compressed into one JSON array (oldest first).

    This is the "read it all at once" tool: every stored message becomes one array item
    that keeps only the main information — message id, date, sender and a truncated gist
    of the text. No reply ids, edit timestamps, raw payloads or event logs are included;
    for those use read_messages / message_events.

    Args:
        chat: subscription reference, e.g. "demo_channel" or "t.me/demo_channel".
        limit: characters of text kept per message (default 280, 0-4000). 0 keeps no text.
        after_id: only messages newer than this Telegram message id.
        include_deleted: also include messages whose Telegram original was deleted.
    """
    _ensure_db()
    limit = max(0, min(int(limit), 4000))
    with session_scope() as session:
        chat_id = _chat_id(session, chat)
        rows = all_messages(
            session, chat_id, after_id=after_id, include_deleted=include_deleted
        )
        return _json(
            [
                {
                    "id": m.message_id,
                    "date": m.date.isoformat() if m.date else None,
                    "from": m.sender_name,
                    **_gist(m.text, limit),
                }
                for m in rows
            ]
        )


_telegram: Any = None


async def _telegram_client() -> Any:
    """Lazily open Telegram using the MCP's OWN session file.

    A separate session keeps this process from contending with the poller over the same
    SQLite session file. The webhook forwarder is attached here, on the same client, so
    one connection serves both the live tools and delivery.

    Note the lifetime: deliveries happen while this process runs. A stdio MCP client
    keeps it alive for the session; an HTTP server keeps it alive for its own life.
    """
    global _telegram
    if _telegram is None:
        from .config import load_settings
        from .telegram_client import build_client
        from .webhooks import register_forwarder

        settings = load_settings()
        client = build_client(settings, settings.mcp_session_path)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise RuntimeError(
                    "Telegram MCP session is not authorized. Run locally: "
                    ".venv/bin/python scripts/telegram_login.py --mcp"
                )
            register_forwarder(client)
        except BaseException:
            await client.disconnect()
            raise
        _telegram = client
    return _telegram


@mcp.tool()
async def list_chats(limit: int = 200) -> str:
    """List the Telegram chats/channels this account can see, most recent first.

    Use this to discover a chat reference for read_messages or send_message.
    Returns chat_id, title, username, kind, unread count.
    """
    from .telegram_ops import list_chats as _list_chats

    limit = max(1, min(int(limit), 1000))
    client = await _telegram_client()
    return _json(await _list_chats(client, limit=limit))


@mcp.tool()
async def read_live_messages(chat: str, limit: int = 50) -> str:
    """Read recent messages directly from Telegram, without PostgreSQL or a subscription."""
    from .telegram_ops import fetch_history

    client = await _telegram_client()
    return _json(await fetch_history(client, chat, limit=max(1, min(int(limit), 1000))))


@mcp.tool()
async def send_message(chat: str, text: str) -> str:
    """Send a text message to a chat. REFUSED unless the chat is in SEND_ALLOWLIST.

    This sends as the real user account, not a bot, so it is denied by default and
    rate-limited. `chat` accepts @username, a t.me link, a numeric id, or a title.
    """
    from .config import load_settings
    from .telegram_ops import SendNotAllowed, assert_send_allowed, send_text

    settings = load_settings()
    try:
        assert_send_allowed(settings, chat)
        client = await _telegram_client()
        result = await send_text(client, settings, chat, text)
    except SendNotAllowed as exc:
        # Return the reason rather than raising: the MCP SDK replaces a raised
        # exception with a generic "Error executing tool", which hides why it failed.
        return _json({"sent": False, "refused": True, "reason": str(exc)})
    return _json({"sent": True, **result})


@mcp.tool()
def archive_stats() -> str:
    """Row counts per table (subscriptions, messages, events, deleted)."""
    _ensure_db()
    with session_scope() as session:
        return _json(store_stats(session))


# ── webhook forwarding: where arriving messages of a chat are POSTed ─────────

def _webhook_row(w: Any) -> dict:
    return {
        "chat_id": w.chat_id,
        "chat": w.chat_ref,
        "url": w.url,
        "enabled": w.enabled,
        "delivered": w.delivered,
        "failed": w.failed,
        "last_error": w.last_error,
        "last_delivered_at": w.last_delivered_at.isoformat() if w.last_delivered_at else None,
    }


def _check_url(url: str) -> str:
    """Reject obvious mistakes early; this URL is dialled by the webhook forwarder."""
    from urllib.parse import urlparse

    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"webhook URL must be an absolute http(s) URL, got {url!r}")
    return url.strip()


def _webhook_auth_error() -> Optional[dict]:
    """Subscribing is gated on WEBHOOK_TOKEN: without it the forwarder is an open SSRF."""
    from .webhooks import webhook_token

    if not webhook_token():
        return {
            "ok": False,
            "refused": True,
            "reason": (
                "WEBHOOK_TOKEN is not set. Forwarding to an arbitrary URL from the Telegram "
                "account is off by default; set WEBHOOK_TOKEN in .env to enable it."
            ),
        }
    return None


@mcp.tool()
def webhook_subscribe(chat: str, url: str) -> str:
    """Forward every message arriving in `chat` to `url` as a raw Telethon update.

    Subscribing the same chat again replaces its URL. Requires WEBHOOK_TOKEN (sent to
    the receiver as the X-Webhook-Token header); without it the call is refused.

    Args:
        chat: subscription reference, @username, t.me link or numeric id.
        url: absolute http(s) URL that receives a JSON POST per message.
    """
    refused = _webhook_auth_error()
    if refused:
        return _json(refused)
    try:
        target = _check_url(url)
    except ValueError as exc:
        return _json({"ok": False, "subscribed": False, "reason": str(exc)})

    _ensure_db()
    with session_scope() as session:
        try:
            chat_id = _chat_id(session, chat)
        except ValueError as exc:
            # A chat that is not in the archive yet can still be watched by its numeric
            # id; anything else needs the id so deliveries can be matched.
            return _json({"ok": False, "subscribed": False, "reason": str(exc)})
        row = upsert_webhook(session, chat_id, target, chat_ref=chat)
        return _json({"ok": True, "subscribed": True, **_webhook_row(row)})


@mcp.tool()
def webhook_unsubscribe(chat: str) -> str:
    """Stop forwarding messages of `chat` to its webhook.

    Delivery stops on the next arriving message — the forwarder re-reads the table for
    every message, so no restart is needed.
    """
    _ensure_db()
    with session_scope() as session:
        try:
            chat_id = _chat_id(session, chat)
        except ValueError as exc:
            return _json({"ok": False, "subscribed": False, "reason": str(exc)})
        removed = delete_webhook(session, chat_id)
        return _json({
            "ok": True,
            "subscribed": False,
            "chat": chat,
            "chat_id": chat_id,
            "was_subscribed": removed,
        })


@mcp.tool()
def webhook_list() -> str:
    """Every chat currently forwarding to a webhook, with delivery counters and last error."""
    _ensure_db()
    with session_scope() as session:
        return _json([_webhook_row(w) for w in list_webhooks(session)])



# ── scoring (read-only: the scorer service writes, MCP only reads) ────────────

def _score(m: PostScore) -> dict:
    return {
        "message_id": m.message_id,
        "status": m.status,
        "date": m.message_date.isoformat() if m.message_date else None,
        "title": m.title,
        "genre": m.genre,
        "primary": m.primary,
        "secondary": m.secondary,
        "thesis": m.main_thesis,
        "scores": {
            axis: getattr(m, f"score_{axis.lower()}")
            for axis in ("I", "V", "U", "P", "S", "D")
        },
        "kpd": m.kpd,
        "eff": m.eff,
        "density_type": m.density_type,
        "flags": m.flags,
        "deviation": m.deviation,
        "trend_position": m.trend_position,
        "shift_candidate": m.shift_candidate,
        "confidence": m.confidence,
        "nearest_anchors": m.nearest_anchors,
        "selfcheck": m.selfcheck,
        "attempts": m.attempts,
        "detail": m.detail,
        "model": m.model,
        "prompt_version": m.prompt_version,
        "scored_at": m.scored_at.isoformat() if m.scored_at else None,
    }


@mcp.tool()
def post_scores(chat: str, limit: int = 20, include_skipped: bool = False) -> str:
    """Rubric scores stored for a chat, newest first.

    Each row is the six axes, KPD/Eff, the classification, and the channel-relative
    view (deviation from the trend, position, shift candidate).
    """
    _ensure_db()
    limit = max(1, min(int(limit), 500))
    with session_scope() as session:
        chat_id = _chat_id(session, chat)
        stmt = select(PostScore).where(PostScore.chat_id == chat_id)
        if not include_skipped:
            stmt = stmt.where(PostScore.status == "scored")
        rows = list(session.exec(stmt.order_by(PostScore.message_id.desc()).limit(limit)).all())
        return _json({"chat": chat, "chat_id": chat_id, "count": len(rows),
                      "scores": [_score(r) for r in rows]})


@mcp.tool()
def channel_states() -> str:
    """The trajectory of every scored channel: profile, trend, summary, tags, anchors."""
    _ensure_db()
    with session_scope() as session:
        states = list(
            session.exec(select(ChannelState).order_by(ChannelState.updated_at.desc())).all()
        )
        pending = pending_counts(session)
        return _json(
            [
                state_snapshot(
                    session,
                    state,
                    context_anchors=5,
                    events=5,
                    pending=pending.get(int(state.chat_id), 0),
                )
                for state in states
            ]
        )


@mcp.tool()
def channel_events(chat: str, limit: int = 20) -> str:
    """Append-only trajectory events for a channel (shift / peak / summary), newest first."""
    _ensure_db()
    limit = max(1, min(int(limit), 200))
    with session_scope() as session:
        chat_id = _chat_id(session, chat)
        rows = list(
            session.exec(
                select(ChannelEvent)
                .where(ChannelEvent.chat_id == chat_id)
                .order_by(ChannelEvent.id.desc())
                .limit(limit)
            ).all()
        )
        return _json({
            "chat": chat,
            "count": len(rows),
            "events": [
                {
                    "kind": e.kind,
                    "at": e.created_at.isoformat() if e.created_at else None,
                    "message_id": e.message_id,
                    "note": e.note,
                }
                for e in rows
            ],
        })


def run(transport: str = "stdio", **kwargs: Any) -> None:
    """Start the MCP server. `transport` is 'stdio' or 'streamable-http'."""
    mcp.run(transport=transport, **kwargs)


if __name__ == "__main__":  # pragma: no cover
    run()
