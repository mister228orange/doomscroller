"""Persistence: record events, read messages back.

Two invariants this module is responsible for:

1. **Idempotent ingest.** Recording the same ``new`` message twice must not create a
   second row and must not append a duplicate event. Re-polling is expected and normal.
2. **Append-only history.** ``message_events`` is never updated or deleted, so the raw
   history survives even when ``messages`` is later edited or marked deleted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, Optional, Sequence

from sqlalchemy import func, or_
from sqlmodel import Session, select

from .models import DetectedEvent, Message, MessageEvent, Subscription, utcnow

VALID_EVENT_TYPES = {"new", "edited", "deleted"}


@dataclass
class RecordResult:
    created: bool
    event_appended: bool
    message_id: int


def _apply_message_fields(msg: Message, **fields: Any) -> None:
    for key, value in fields.items():
        if value is not None and hasattr(msg, key):
            setattr(msg, key, value)


def record_event(
    session: Session,
    *,
    chat_id: int,
    message_id: int,
    event_type: str,
    text: Optional[str] = None,
    date: Optional[datetime] = None,
    sender_id: Optional[int] = None,
    sender_name: Optional[str] = None,
    reply_to_message_id: Optional[int] = None,
    payload: Optional[dict] = None,
) -> RecordResult:
    """Record one Telegram event. Safe to call repeatedly with the same input."""
    if event_type not in VALID_EVENT_TYPES:
        raise ValueError(f"event_type must be one of {sorted(VALID_EVENT_TYPES)}, got {event_type!r}")

    existing = session.exec(
        select(Message).where(Message.chat_id == chat_id, Message.message_id == message_id)
    ).first()

    # Re-polling re-delivers `new`; that must be a no-op, not a duplicate.
    if event_type == "new" and existing is not None:
        return RecordResult(created=False, event_appended=False, message_id=message_id)

    session.add(
        MessageEvent(
            chat_id=chat_id,
            message_id=message_id,
            event_type=event_type,
            text=text,
            payload=payload,
        )
    )

    created = existing is None
    if created:
        msg = Message(chat_id=chat_id, message_id=message_id)
        _apply_message_fields(
            msg,
            text=text,
            date=date,
            sender_id=sender_id,
            sender_name=sender_name,
            reply_to_message_id=reply_to_message_id,
            raw=payload,
        )
        session.add(msg)
    else:
        msg = existing
        # A delete must not wipe the text we already archived.
        if event_type != "deleted":
            _apply_message_fields(
                msg,
                text=text,
                date=date if msg.date is None else None,
                sender_id=sender_id,
                sender_name=sender_name,
                reply_to_message_id=reply_to_message_id,
                raw=payload,
            )
        msg.updated_at = utcnow()

    if event_type == "deleted":
        msg.deleted = True
    elif event_type == "edited":
        msg.edited_at = utcnow()

    session.flush()
    return RecordResult(created=created, event_appended=True, message_id=message_id)


# ── reading ───────────────────────────────────────────────────────────────────

def _message_query(chat_id: int, include_deleted: bool):
    stmt = select(Message).where(Message.chat_id == chat_id)
    if not include_deleted:
        stmt = stmt.where(Message.deleted == False)  # noqa: E712
    return stmt


def fetch_messages(
    session: Session,
    chat_id: int,
    limit: int = 50,
    *,
    before_id: Optional[int] = None,
    after_id: Optional[int] = None,
    include_deleted: bool = False,
    newest_first: bool = True,
) -> list[Message]:
    """Read up to ``limit`` stored messages.

    ``before_id`` pages backwards from a message id (older), ``after_id`` forwards
    (newer). Both are Telegram message ids, not primary keys.
    """
    stmt = _message_query(chat_id, include_deleted)
    if before_id is not None:
        stmt = stmt.where(Message.message_id < before_id)
    if after_id is not None:
        stmt = stmt.where(Message.message_id > after_id)
    order = Message.message_id.desc() if newest_first else Message.message_id.asc()
    stmt = stmt.order_by(order).limit(limit)
    return list(session.exec(stmt).all())


def iter_all_messages(
    session: Session,
    chat_id: int,
    *,
    after_id: Optional[int] = None,
    batch_size: int = 1000,
    include_deleted: bool = False,
) -> Iterator[Message]:
    """Stream the entire stored history, oldest first, without loading it all.

    This is how "any amount of saved messages" is served without a limit.
    """
    cursor = after_id
    while True:
        stmt = _message_query(chat_id, include_deleted)
        if cursor is not None:
            stmt = stmt.where(Message.message_id > cursor)
        stmt = stmt.order_by(Message.message_id.asc()).limit(batch_size)
        rows = list(session.exec(stmt).all())
        if not rows:
            return
        for row in rows:
            yield row
        cursor = rows[-1].message_id
        if len(rows) < batch_size:
            return


def new_messages(
    session: Session,
    chat_id: int,
    since_id: Optional[int] = None,
    *,
    include_deleted: bool = False,
) -> list[Message]:
    """Everything newer than ``since_id``, or newer than the subscription cursor."""
    if since_id is None:
        sub = session.exec(select(Subscription).where(Subscription.chat_id == chat_id)).first()
        since_id = sub.cursor_message_id if sub else 0
    return fetch_messages(
        session,
        chat_id,
        limit=100000,
        after_id=since_id,
        include_deleted=include_deleted,
        newest_first=False,
    )


def search_messages(
    session: Session,
    query: str,
    *,
    chat_id: Optional[int] = None,
    limit: int = 50,
    include_deleted: bool = False,
) -> list[Message]:
    """Case-insensitive substring search across stored messages."""
    stmt = select(Message)
    if chat_id is not None:
        stmt = stmt.where(Message.chat_id == chat_id)
    if not include_deleted:
        stmt = stmt.where(Message.deleted == False)  # noqa: E712
    like = f"%{query}%"
    stmt = stmt.where(or_(Message.text.ilike(like), Message.sender_name.ilike(like)))
    return list(session.exec(stmt.order_by(Message.message_id.desc()).limit(limit)).all())


def events_for_message(session: Session, chat_id: int, message_id: int) -> list[MessageEvent]:
    return list(
        session.exec(
            select(MessageEvent)
            .where(MessageEvent.chat_id == chat_id, MessageEvent.message_id == message_id)
            .order_by(MessageEvent.id.asc())
        ).all()
    )


# ── aggregates ────────────────────────────────────────────────────────────────

def stats(session: Session, chat_id: Optional[int] = None) -> dict:
    def count(model, *where):
        stmt = select(func.count()).select_from(model)
        for w in where:
            stmt = stmt.where(w)
        return int(session.exec(stmt).one())

    out: dict = {
        "subscriptions": count(Subscription),
        "subscriptions_enabled": count(Subscription, Subscription.enabled == True),  # noqa: E712
        "messages": count(Message),
        "message_events": count(MessageEvent),
        "detected_events": count(DetectedEvent),
        "deleted_messages": count(Message, Message.deleted == True),  # noqa: E712
    }
    if chat_id is not None:
        out["messages_in_chat"] = count(Message, Message.chat_id == chat_id)
    return out
