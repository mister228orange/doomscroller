"""Live Telegram operations: list chats, read history, send a message.

Reads are unrestricted (they only need the session). **Sends are denied by default** and
must be explicitly allowlisted via ``SEND_ALLOWLIST`` — because this acts as a real user
account, not a bot, and programmatic sending can get an account limited or banned.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Optional

from telethon import TelegramClient
from telethon.tl.types import Channel, Chat, User

from .config import Settings

logger = logging.getLogger(__name__)

# Simple in-process rate limiter: timestamps of recent sends.
_send_times: list[float] = []


class SendNotAllowed(PermissionError):
    """Raised when a send is blocked by the allowlist (or sending is off)."""


def _kind(entity: Any) -> str:
    if isinstance(entity, Channel):
        return "channel" if getattr(entity, "broadcast", False) else "supergroup"
    if isinstance(entity, Chat):
        return "group"
    if isinstance(entity, User):
        return "bot" if getattr(entity, "bot", False) else "user"
    return "unknown"


def chat_row(dialog: Any) -> dict:
    """Normalise a Telethon dialog into a plain dict."""
    entity = dialog.entity
    return {
        "chat_id": getattr(entity, "id", None),
        "title": dialog.name,
        "username": getattr(entity, "username", None),
        "kind": _kind(entity),
        "is_bot": bool(getattr(entity, "bot", False)),
        "unread_count": getattr(dialog, "unread_count", 0),
        "last_message_date": dialog.date.isoformat() if dialog.date else None,
    }


def message_row(message: Any) -> dict:
    """Normalise a Telethon message into a plain dict (JSON-safe)."""
    sender = getattr(message, "sender", None)
    sender_name = None
    if sender is not None:
        sender_name = (
            getattr(sender, "title", None)
            or " ".join(
                p for p in (getattr(sender, "first_name", None), getattr(sender, "last_name", None)) if p
            )
            or getattr(sender, "username", None)
        )
    return {
        "message_id": message.id,
        "date": message.date.isoformat() if message.date else None,
        "sender_id": getattr(message, "sender_id", None),
        "sender_name": sender_name,
        "text": getattr(message, "text", None),
        "reply_to_message_id": getattr(message, "reply_to_msg_id", None),
        "has_media": bool(getattr(message, "media", None)),
        "views": getattr(message, "views", None),
        "forwards": getattr(message, "forwards", None),
    }


async def list_chats(client: TelegramClient, limit: int = 200) -> list[dict]:
    """List dialogs the account can see, most recent first."""
    dialogs = await client.get_dialogs(limit=limit)
    return [chat_row(d) for d in dialogs]


async def resolve_chat(client: TelegramClient, ref: str) -> Any:
    """Resolve @name / t.me link / numeric id / title to an entity."""
    candidate: Any = ref
    stripped = ref.strip()
    for prefix in ("https://", "http://"):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):]
    if stripped.startswith("t.me/"):
        stripped = stripped[len("t.me/"):]
    stripped = stripped.strip("/")
    if stripped.startswith("@"):
        stripped = stripped[1:]
    try:
        if stripped.lstrip("-").isdigit():
            candidate = int(stripped)
        else:
            candidate = stripped
    except ValueError:
        candidate = stripped
    return await client.get_entity(candidate)


async def history_for_entity(client: TelegramClient, entity: Any, limit: int = 100, label: Optional[str] = None) -> dict:
    """Last `limit` messages of an already-resolved entity, oldest→newest."""
    messages = await client.get_messages(entity, limit=limit)
    rows = [message_row(m) for m in reversed(messages) if m is not None]
    return {
        "chat_id": getattr(entity, "id", None),
        "title": (getattr(entity, "title", None) or getattr(entity, "username", None)
                  or getattr(entity, "first_name", None) or label),
        "username": getattr(entity, "username", None),
        "kind": _kind(entity),
        "message_count": len(rows),
        "messages": rows,
    }


async def fetch_history(client: TelegramClient, ref: str, limit: int = 100) -> dict:
    """Last `limit` messages of a chat identified by ref."""
    entity = await resolve_chat(client, ref)
    return await history_for_entity(client, entity, limit=limit, label=ref)


def _check_rate(settings: Settings) -> None:
    now = time.time()
    cutoff = now - 60
    while _send_times and _send_times[0] < cutoff:
        _send_times.pop(0)
    if len(_send_times) >= settings.send_rate_per_min:
        raise SendNotAllowed(
            f"rate limit: {settings.send_rate_per_min} sends/minute exceeded — wait a moment"
        )


def assert_send_allowed(settings: Settings, chat_ref: str) -> None:
    """Raise SendNotAllowed unless this chat is explicitly allowlisted."""
    if not settings.send_allowlist:
        raise SendNotAllowed(
            "sending is disabled: set SEND_ALLOWLIST in .env to the chat(s) you allow, "
            "or '*' to allow all. Sending from a user account is off by default."
        )
    if not settings.send_allowed(chat_ref):
        raise SendNotAllowed(
            f"sending to {chat_ref!r} is not allowed. Allowed: {settings.send_allowlist}"
        )


async def send_text(
    client: TelegramClient, settings: Settings, chat: str, text: str
) -> dict:
    """Send a text message. Refuses unless allowlisted, and rate-limits."""
    if not text or not text.strip():
        raise ValueError("refusing to send an empty message")
    assert_send_allowed(settings, chat)
    _check_rate(settings)

    entity = await resolve_chat(client, chat)
    sent = await client.send_message(entity, text)
    _send_times.append(time.time())

    logger.info("sent message %s to %s", getattr(sent, "id", "?"), chat)
    return {
        "chat": chat,
        "chat_id": getattr(entity, "id", None),
        "message_id": getattr(sent, "id", None),
        "date": sent.date.isoformat() if getattr(sent, "date", None) else None,
        "text": text,
    }
