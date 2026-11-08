"""Telethon client construction, including MTProto/SOCKS5 proxy support."""

from __future__ import annotations

from pathlib import Path

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


async def login_flow(
    client: TelegramClient,
    settings: Settings,
    *,
    code: str | None = None,
    phone_code_hash: str | None = None,
) -> dict[str, str]:
    """One or both phases of a terminal login.

    Phase 1 (no ``code``): request the login code and return the phone_code_hash the
    caller must hand back to phase 2 — Telethon keeps it in memory only, so a two-command
    login needs it echoed. Phase 2: sign in with the code; two-factor accounts are asked
    for the password via getpass. A session revoked by Telegram
    (``AuthKeyDuplicatedError`` — the key was seen from two IPs) is moved aside so the
    fresh login starts on a clean file instead of dying on connect.
    """
    import getpass
    from datetime import datetime, timezone

    from telethon.errors import AuthKeyDuplicatedError, SessionPasswordNeededError

    try:
        if not client.is_connected():
            await client.connect()
    except AuthKeyDuplicatedError:
        stale = Path(str(client.session.filename or ""))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        moved = stale.with_name(f"{stale.stem}.revoked-{stamp}{stale.suffix}")
        stale.rename(moved)
        await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        return {"status": "already authorized", "user": getattr(me, "username", None) or ""}
    if not code:
        sent = await client.send_code_request(settings.phone)
        phone_code_hash = sent.phone_code_hash or ""
        import sys

        if sys.stdin.isatty():
            # A human at the terminal: ask for the code right here, same process, the
            # hash never has to leave memory.
            code = input("Код из Telegram: ").strip()
        else:
            return {
                "status": "code sent",
                "phone_code_hash": phone_code_hash,
                "next": "doomscroller login --code <код> --hash <phone_code_hash>",
            }
    try:
        await client.sign_in(phone=settings.phone, code=code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        password = getpass.getpass("2FA password: ")
        await client.sign_in(password=password)
    me = await client.get_me()
    return {"status": "logged in", "user": getattr(me, "username", None) or ""}
