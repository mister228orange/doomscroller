"""Database models.

Four tables, deliberately:

* ``subscriptions``   — which chats/channels we follow, and the ingest cursor.
* ``messages``        — current state of every message (upserted on edit).
* ``message_events``  — append-only log of every raw Telegram event
                        (new / edited / deleted). Nothing is ever overwritten here.
* ``detected_events`` — optional LLM-detected events (see ``detector.py``).

The split matters: ``messages`` answers "what does this chat look like now",
``message_events`` answers "what actually happened, in order". Keeping the append-only
log separate is what makes "saving all events" true rather than aspirational.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Subscription(SQLModel, table=True):
    """A chat/channel we archive. ``chat_ref`` is what the user typed;
    ``chat_id`` is the resolved numeric id once we have talked to Telegram."""

    __tablename__ = "subscriptions"

    id: Optional[int] = Field(default=None, primary_key=True)
    chat_ref: str = Field(
        sa_column=Column("chat_ref", String(255), nullable=False, unique=True, index=True)
    )
    chat_id: Optional[int] = Field(
        default=None, sa_column=Column("chat_id", BigInteger, nullable=True, index=True)
    )
    title: Optional[str] = Field(default=None, sa_column=Column("title", Text, nullable=True))
    kind: Optional[str] = Field(
        default=None, sa_column=Column("kind", String(32), nullable=True)
    )
    enabled: bool = Field(
        default=True, sa_column=Column("enabled", Boolean, nullable=False, server_default="true")
    )
    # Cursor for poll-based ingest. Only ever advanced *after* a batch is stored.
    cursor_message_id: int = Field(
        default=0,
        sa_column=Column(
            "cursor_message_id", BigInteger, nullable=False, server_default="0"
        ),
    )
    last_polled_at: Optional[datetime] = Field(
        default=None, sa_column=Column("last_polled_at", DateTime(timezone=True), nullable=True)
    )
    last_error: Optional[str] = Field(
        default=None, sa_column=Column("last_error", Text, nullable=True)
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False),
    )
    message_count: int = Field(
        default=0, sa_column=Column("message_count", BigInteger, nullable=False, server_default="0")
    )


class Message(SQLModel, table=True):
    """Current state of a message. One row per (chat_id, message_id)."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", name="uq_messages_chat_message"),
        Index("ix_messages_chat_id_id", "chat_id", "message_id"),
        Index("ix_messages_date", "date"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: int = Field(sa_column=Column("chat_id", BigInteger, nullable=False))
    message_id: int = Field(sa_column=Column("message_id", BigInteger, nullable=False))
    sender_id: Optional[int] = Field(
        default=None, sa_column=Column("sender_id", BigInteger, nullable=True)
    )
    sender_name: Optional[str] = Field(
        default=None, sa_column=Column("sender_name", Text, nullable=True)
    )
    date: Optional[datetime] = Field(
        default=None, sa_column=Column("date", DateTime(timezone=True), nullable=True)
    )
    text: Optional[str] = Field(default=None, sa_column=Column("text", Text, nullable=True))
    reply_to_message_id: Optional[int] = Field(
        default=None, sa_column=Column("reply_to_message_id", BigInteger, nullable=True)
    )
    deleted: bool = Field(
        default=False, sa_column=Column("deleted", Boolean, nullable=False, server_default="false")
    )
    edited_at: Optional[datetime] = Field(
        default=None, sa_column=Column("edited_at", DateTime(timezone=True), nullable=True)
    )
    raw: Optional[dict[str, Any]] = Field(
        default=None, sa_column=Column("raw", JSON, nullable=True)
    )
    first_seen_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column("first_seen_at", DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column("updated_at", DateTime(timezone=True), nullable=False),
    )


class MessageEvent(SQLModel, table=True):
    """Append-only log. Never updated, never deleted.

    ``event_type`` is one of: ``new``, ``edited``, ``deleted``.
    """

    __tablename__ = "message_events"
    __table_args__ = (
        Index("ix_message_events_chat_id_id", "chat_id", "message_id"),
        Index("ix_message_events_created", "created_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: int = Field(sa_column=Column("chat_id", BigInteger, nullable=False))
    message_id: int = Field(sa_column=Column("message_id", BigInteger, nullable=False))
    event_type: str = Field(sa_column=Column("event_type", String(16), nullable=False))
    text: Optional[str] = Field(default=None, sa_column=Column("text", Text, nullable=True))
    payload: Optional[dict[str, Any]] = Field(
        default=None, sa_column=Column("payload", JSON, nullable=True)
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False),
    )


class DetectedEvent(SQLModel, table=True):
    """Events a model decided are interesting (optional feature)."""

    __tablename__ = "detected_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: int = Field(sa_column=Column("chat_id", BigInteger, nullable=False, index=True))
    subscription_id: Optional[int] = Field(
        default=None,
        sa_column=Column("subscription_id", Integer, ForeignKey("subscriptions.id"), nullable=True),
    )
    event: str = Field(sa_column=Column("event", Text, nullable=False))
    message: Optional[str] = Field(
        default=None, sa_column=Column("message", Text, nullable=True)
    )
    state_before: Optional[str] = Field(
        default=None, sa_column=Column("state_before", Text, nullable=True)
    )
    state_after: Optional[str] = Field(
        default=None, sa_column=Column("state_after", Text, nullable=True)
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False),
    )
