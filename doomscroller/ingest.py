"""Pulling messages from Telegram into PostgreSQL.

Two modes:

* **poll** — ask Telegram for everything newer than the stored cursor and store it.
  Resumable, survives restarts, works with a single session.
* **watch** — stay connected and store events as they arrive (new / edited / deleted).

The important property in both: the cursor and the messages it covers are committed
**in the same transaction**. If storing a batch fails, the cursor does not advance, so
the next run re-reads that batch. A crash causes re-processing, never silent data loss.

(This is the bug the previous implementation had: it persisted `last_msg_id` *before*
calling the model, so any failure dropped those messages permanently.)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Optional

from sqlmodel import Session

from .config import Settings
from .db import session_scope
from .models import Subscription, utcnow
from .store import record_event
from .subscriptions import list_subscriptions
from .telegram_client import describe_entity

logger = logging.getLogger(__name__)


def serialize_message(message: Any) -> dict:
    """A JSON-safe snapshot of a Telethon message (no lazy objects, no bytes)."""
    def iso(value: Any) -> Optional[str]:
        return value.isoformat() if isinstance(value, datetime) else None

    out: dict = {
        "id": getattr(message, "id", None),
        "date": iso(getattr(message, "date", None)),
        "edit_date": iso(getattr(message, "edit_date", None)),
        "text": getattr(message, "text", None),
        "sender_id": getattr(message, "sender_id", None),
        "reply_to_msg_id": getattr(message, "reply_to_msg_id", None),
        "views": getattr(message, "views", None),
        "forwards": getattr(message, "forwards", None),
    }
    try:
        sender = message.sender
        if sender is not None:
            out["sender_username"] = getattr(sender, "username", None)
            out["sender_title"] = (
                getattr(sender, "title", None)
                or " ".join(
                    p for p in (getattr(sender, "first_name", None), getattr(sender, "last_name", None)) if p
                )
                or None
            )
    except Exception:  # sender may not be resolvable without another request
        pass
    return out


def _sender_name(message: Any) -> Optional[str]:
    try:
        sender = message.sender
        if sender is None:
            return None
        return (
            getattr(sender, "title", None)
            or " ".join(
                p for p in (getattr(sender, "first_name", None), getattr(sender, "last_name", None)) if p
            )
            or getattr(sender, "username", None)
        )
    except Exception:
        return None


async def resolve_entity(client: Any, ref: str) -> Any:
    """Resolve @username / t.me link / numeric id to a Telethon entity."""
    candidate: Any = ref
    try:
        if ref.lstrip("-").isdigit():
            candidate = int(ref)
    except Exception:
        pass
    return await client.get_entity(candidate)


async def poll_subscription(
    client: Any,
    session: Session,
    sub: Subscription,
    settings: Settings,
) -> int:
    """Store everything new for one subscription. Returns count of new messages."""
    if not sub.enabled:
        return 0

    entity = await resolve_entity(client, sub.chat_ref)
    title, kind, chat_id = describe_entity(entity)
    if chat_id is None:
        raise RuntimeError(f"could not resolve chat id for {sub.chat_ref!r}")
    if sub.chat_id is None:
        sub.chat_id = chat_id
    if title and not sub.title:
        sub.title = title
    if kind and not sub.kind:
        sub.kind = kind

    cursor = int(sub.cursor_message_id or 0)
    created_total = 0

    while True:
        batch = await client.get_messages(
            entity, limit=settings.batch_size, min_id=cursor, reverse=True
        )
        batch = [m for m in batch if m is not None and getattr(m, "id", None)]
        if not batch:
            break
        batch.sort(key=lambda m: m.id)

        created = 0
        for message in batch:
            result = record_event(
                session,
                chat_id=chat_id,
                message_id=message.id,
                event_type="new",
                text=getattr(message, "text", None),
                date=getattr(message, "date", None),
                sender_id=getattr(message, "sender_id", None),
                sender_name=_sender_name(message),
                reply_to_message_id=getattr(message, "reply_to_msg_id", None),
                payload=serialize_message(message),
            )
            if result.created:
                created += 1

        # Cursor advances in the SAME commit as the data it covers.
        cursor = batch[-1].id
        sub.cursor_message_id = cursor
        sub.last_polled_at = utcnow()
        sub.message_count = int(sub.message_count or 0) + created
        sub.last_error = None
        session.commit()
        created_total += created

        if len(batch) < settings.batch_size:
            break

    return created_total


async def poll_all(client: Any, settings: Settings) -> dict:
    """Poll every enabled subscription. One failure does not stop the rest."""
    results: dict[str, Any] = {}
    with session_scope() as session:
        subs = list_subscriptions(session, enabled_only=True)
        refs = [s.chat_ref for s in subs]

    for ref in refs:
        with session_scope() as session:
            sub = session.query(Subscription).filter(Subscription.chat_ref == ref).one()
            try:
                count = await poll_subscription(client, session, sub, settings)
                results[ref] = {"ok": True, "new": count}
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                sub = session.query(Subscription).filter(Subscription.chat_ref == ref).one()
                sub.last_error = f"{type(exc).__name__}: {exc}"
                session.commit()
                logger.warning("poll failed for %s: %s", ref, exc)
                results[ref] = {"ok": False, "error": str(exc)}
    return results


async def watch(client: Any, settings: Settings) -> None:
    """Stay connected and store new / edited / deleted events as they happen."""
    from telethon import events

    with session_scope() as session:
        subs = list_subscriptions(session, enabled_only=True)
        refs = list(subs)
        entity_ids = []
        for sub in refs:
            try:
                entity = await resolve_entity(client, sub.chat_ref)
                _, kind, chat_id = describe_entity(entity)
                if chat_id:
                    sub.chat_id = chat_id
                    entity_ids.append(chat_id)
            except Exception as exc:  # noqa: BLE001
                sub.last_error = f"resolve failed: {exc}"
                logger.warning("cannot watch %s: %s", sub.chat_ref, exc)

    if not entity_ids:
        raise RuntimeError("no watchable subscriptions — add one with `subscribe` first")

    logger.info("watching %d chat(s)", len(entity_ids))

    @client.on(events.NewMessage(chats=entity_ids))
    async def _on_new(event):  # noqa: ANN001
        message = event.message
        with session_scope() as session:
            record_event(
                session,
                chat_id=event.chat_id,
                message_id=message.id,
                event_type="new",
                text=getattr(message, "text", None),
                date=getattr(message, "date", None),
                sender_id=getattr(message, "sender_id", None),
                sender_name=_sender_name(message),
                reply_to_message_id=getattr(message, "reply_to_msg_id", None),
                payload=serialize_message(message),
            )

    @client.on(events.MessageEdited(chats=entity_ids))
    async def _on_edited(event):  # noqa: ANN001
        message = event.message
        with session_scope() as session:
            record_event(
                session,
                chat_id=event.chat_id,
                message_id=message.id,
                event_type="edited",
                text=getattr(message, "text", None),
                date=getattr(message, "date", None),
                sender_id=getattr(message, "sender_id", None),
                sender_name=_sender_name(message),
                payload=serialize_message(message),
            )

    @client.on(events.MessageDeleted())
    async def _on_deleted(event):  # noqa: ANN001
        chat_id = getattr(event, "chat_id", None)
        if not chat_id:
            logger.debug("delete event without chat_id; skipping")
            return
        with session_scope() as session:
            for message_id in getattr(event, "deleted_ids", []) or []:
                record_event(
                    session,
                    chat_id=chat_id,
                    message_id=message_id,
                    event_type="deleted",
                    payload={"source": "watch"},
                )

    await client.run_until_disconnected()


async def run_poll_loop(client: Any, settings: Settings) -> None:
    """Poll forever on an interval — this is what the compose service runs."""
    logger.info("poll loop every %ss", settings.poll_interval)
    while True:
        try:
            results = await poll_all(client, settings)
            logger.info("poll: %s", results)
        except Exception as exc:  # noqa: BLE001
            logger.exception("poll cycle failed: %s", exc)
        await asyncio.sleep(settings.poll_interval)
