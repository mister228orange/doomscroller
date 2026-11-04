"""Telethon client construction, including MTProto/SOCKS5 proxy support."""

from __future__ import annotations

import logging
from typing import Any, Optional

from telethon import TelegramClient

from .config import Settings

logger = logging.getLogger(__name__)


def build_client(settings: Settings, session_path: Any | None = None) -> TelegramClient:
    """Build a Telethon client.

    ``session_path`` lets the MCP server use its own session file so it never contends
    with the poller for the same SQLite session.
    """
    kwargs: dict[str, Any] = settings.proxy.telethon_kwargs()
    logger.info("Telegram connecting via %s", settings.proxy.describe())
    return TelegramClient(
        str(session_path or settings.session_path),
        settings.api_id,
        settings.api_hash,
        **kwargs,
    )


async def ensure_connected(client: TelegramClient, settings: Settings) -> None:
    """Connect and authorise. Uses the stored session when present."""
    if not client.is_connected():
        await client.connect()
    if not await client.is_user_authorized():
        if settings.phone:
            await client.start(phone=settings.phone)
        else:
            raise RuntimeError(
                "Telegram session is not authorised and TELEGRAM_PHONE is not set. "
                "Run the login flow once (see README) or set TELEGRAM_PHONE."
            )


def describe_entity(entity: Any) -> tuple[Optional[str], Optional[str], Optional[int]]:
    """Return (title, kind, chat_id) for a Telethon entity."""
    from telethon.tl.types import Channel, Chat, User

    chat_id = getattr(entity, "id", None)
    if isinstance(entity, Channel):
        kind = "channel" if getattr(entity, "broadcast", False) else "group"
    elif isinstance(entity, Chat):
        kind = "group"
    elif isinstance(entity, User):
        kind = "user"
    else:
        kind = type(entity).__name__.lower()
    title = (
        getattr(entity, "title", None)
        or getattr(entity, "username", None)
        or (getattr(entity, "first_name", "") or "")
    )
    return (title or None), kind, chat_id
