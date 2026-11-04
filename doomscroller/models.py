"""Database models.

Four tables for the archive, deliberately:

* ``subscriptions``   — which chats/channels we follow, and the ingest cursor.
* ``messages``        — current state of every message (upserted on edit).
* ``message_events``  — append-only log of every raw Telegram event
                        (new / edited / deleted). Nothing is ever overwritten here.
* ``detected_events`` — optional LLM-detected events (see ``detector.py``).

The split matters: ``messages`` answers "what does this chat look like now",
``message_events`` answers "what actually happened, in order". Keeping the append-only
log separate is what makes "saving all events" true rather than aspirational.

Three more tables hold the scoring of the archive (see ``doomscroller/scoring``):

* ``post_scores``     — one rubric row per scored message. ``status`` is
                        ``scored`` / ``skipped`` / ``failed``, so "unread" is a query,
                        not a second cursor that can drift.
* ``channel_states``  — the rolling trajectory of one channel: moving means, tag
                        distribution, local anchors, rolling summary, counters.
* ``channel_events``  — append-only log of trajectory events (shift / peak / summary).
                        Same rule as ``message_events``: never updated, never deleted.
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
    Float,
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


# ── scoring ───────────────────────────────────────────────────────────────────


class PostScore(SQLModel, table=True):
    """One rubric row per post.

    ``status`` makes "what has not been scored yet" a query rather than a second
    cursor: ``scored`` rows are done, ``skipped`` rows are deliberately not rubric
    material (too short / no text), ``failed`` rows are retried up to a limit.
    """

    __tablename__ = "post_scores"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", name="uq_post_scores_chat_message"),
        Index("ix_post_scores_chat_kpd", "chat_id", "kpd"),
        Index("ix_post_scores_scored_at", "scored_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: int = Field(sa_column=Column("chat_id", BigInteger, nullable=False))
    message_id: int = Field(sa_column=Column("message_id", BigInteger, nullable=False))
    message_date: Optional[datetime] = Field(
        default=None, sa_column=Column("message_date", DateTime(timezone=True), nullable=True)
    )
    title: Optional[str] = Field(default=None, sa_column=Column("title", Text, nullable=True))

    status: str = Field(
        default="scored",
        sa_column=Column("status", String(16), nullable=False, server_default="scored", index=True),
    )
    detail: Optional[str] = Field(default=None, sa_column=Column("detail", Text, nullable=True))
    attempts: int = Field(
        default=0, sa_column=Column("attempts", Integer, nullable=False, server_default="0")
    )

    # Provenance — a score is only reproducible together with the prompt that produced it.
    model: Optional[str] = Field(default=None, sa_column=Column("model", Text, nullable=True))
    prompt_version: Optional[str] = Field(
        default=None, sa_column=Column("prompt_version", String(64), nullable=True)
    )

    genre: Optional[str] = Field(default=None, sa_column=Column("genre", String(64), nullable=True))
    primary: Optional[str] = Field(default=None, sa_column=Column("primary", String(16), nullable=True))
    secondary: Optional[list[str]] = Field(
        default=None, sa_column=Column("secondary", JSON, nullable=True)
    )
    main_thesis: Optional[str] = Field(
        default=None, sa_column=Column("main_thesis", Text, nullable=True)
    )
    clauses: Optional[dict[str, Any]] = Field(
        default=None, sa_column=Column("clauses", JSON, nullable=True)
    )
    density_type: Optional[str] = Field(
        default=None, sa_column=Column("density_type", String(16), nullable=True)
    )
    flags: Optional[list[str]] = Field(default=None, sa_column=Column("flags", JSON, nullable=True))

    # The six axes as real columns, so they can be filtered and aggregated in SQL.
    score_i: Optional[float] = Field(default=None, sa_column=Column("score_i", Float, nullable=True))
    score_v: Optional[float] = Field(default=None, sa_column=Column("score_v", Float, nullable=True))
    score_u: Optional[float] = Field(default=None, sa_column=Column("score_u", Float, nullable=True))
    score_p: Optional[float] = Field(default=None, sa_column=Column("score_p", Float, nullable=True))
    score_s: Optional[float] = Field(default=None, sa_column=Column("score_s", Float, nullable=True))
    score_d: Optional[float] = Field(default=None, sa_column=Column("score_d", Float, nullable=True))

    kpd: Optional[float] = Field(default=None, sa_column=Column("kpd", Float, nullable=True))
    eff: Optional[float] = Field(default=None, sa_column=Column("eff", Float, nullable=True))

    # Channel-relative view (see scoring/08_channel_context.md).
    deviation: Optional[dict[str, float]] = Field(
        default=None, sa_column=Column("deviation", JSON, nullable=True)
    )
    trend_position: Optional[str] = Field(
        default=None, sa_column=Column("trend_position", String(16), nullable=True)
    )
    shift_candidate: bool = Field(
        default=False,
        sa_column=Column("shift_candidate", Boolean, nullable=False, server_default="false"),
    )
    confidence: Optional[str] = Field(
        default=None, sa_column=Column("confidence", String(16), nullable=True)
    )

    justification: Optional[dict[str, str]] = Field(
        default=None, sa_column=Column("justification", JSON, nullable=True)
    )
    nearest_anchors: Optional[list[str]] = Field(
        default=None, sa_column=Column("nearest_anchors", JSON, nullable=True)
    )
    selfcheck: Optional[list[str]] = Field(
        default=None, sa_column=Column("selfcheck", JSON, nullable=True)
    )
    raw: Optional[dict[str, Any]] = Field(default=None, sa_column=Column("raw", JSON, nullable=True))

    scored_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column("scored_at", DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column("updated_at", DateTime(timezone=True), nullable=False),
    )


class ChannelState(SQLModel, table=True):
    """The rolling trajectory of one channel.

    This is the ``state/{{channel}}.json`` of the methodology, kept in the database so
    the scorer service and every reader share one copy. ``post_scores`` is the full
    history; this row is the derived projection the next scoring call reads.
    """

    __tablename__ = "channel_states"

    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: int = Field(
        sa_column=Column("chat_id", BigInteger, nullable=False, unique=True, index=True)
    )
    chat_ref: Optional[str] = Field(default=None, sa_column=Column("chat_ref", String(255), nullable=True))
    title: Optional[str] = Field(default=None, sa_column=Column("title", Text, nullable=True))

    profile_description: Optional[str] = Field(
        default=None, sa_column=Column("profile_description", Text, nullable=True)
    )
    primary_dominant: Optional[str] = Field(
        default=None, sa_column=Column("primary_dominant", String(16), nullable=True)
    )
    secondary_dominant: Optional[list[str]] = Field(
        default=None, sa_column=Column("secondary_dominant", JSON, nullable=True)
    )
    summary: Optional[str] = Field(default=None, sa_column=Column("summary", Text, nullable=True))
    drift_note: Optional[str] = Field(default=None, sa_column=Column("drift_note", Text, nullable=True))
    summary_at: Optional[datetime] = Field(
        default=None, sa_column=Column("summary_at", DateTime(timezone=True), nullable=True)
    )
    summary_post_count: int = Field(
        default=0,
        sa_column=Column("summary_post_count", Integer, nullable=False, server_default="0"),
    )

    window: int = Field(
        default=5, sa_column=Column("window", Integer, nullable=False, server_default="5")
    )
    means: Optional[dict[str, float]] = Field(default=None, sa_column=Column("means", JSON, nullable=True))
    deltas: Optional[dict[str, float]] = Field(default=None, sa_column=Column("deltas", JSON, nullable=True))
    kpd_mean: Optional[float] = Field(default=None, sa_column=Column("kpd_mean", Float, nullable=True))
    eff_mean: Optional[float] = Field(default=None, sa_column=Column("eff_mean", Float, nullable=True))
    # Eff is undefined for weak texts (KPD <= 5), so its mean averages only defined values.
    eff_n: int = Field(
        default=0, sa_column=Column("eff_n", Integer, nullable=False, server_default="0")
    )

    total_scored: int = Field(
        default=0, sa_column=Column("total_scored", Integer, nullable=False, server_default="0")
    )
    total_skipped: int = Field(
        default=0, sa_column=Column("total_skipped", Integer, nullable=False, server_default="0")
    )
    total_failed: int = Field(
        default=0, sa_column=Column("total_failed", Integer, nullable=False, server_default="0")
    )
    last_scored_message_id: Optional[int] = Field(
        default=None, sa_column=Column("last_scored_message_id", BigInteger, nullable=True)
    )
    last_scored_at: Optional[datetime] = Field(
        default=None, sa_column=Column("last_scored_at", DateTime(timezone=True), nullable=True)
    )

    tag_distribution: Optional[dict[str, int]] = Field(
        default=None, sa_column=Column("tag_distribution", JSON, nullable=True)
    )
    local_anchors: Optional[list[dict[str, Any]]] = Field(
        default=None, sa_column=Column("local_anchors", JSON, nullable=True)
    )

    deviation_streak: int = Field(
        default=0,
        sa_column=Column("deviation_streak", Integer, nullable=False, server_default="0"),
    )
    shift_candidate_message_id: Optional[int] = Field(
        default=None, sa_column=Column("shift_candidate_message_id", BigInteger, nullable=True)
    )
    kpd_max: Optional[float] = Field(default=None, sa_column=Column("kpd_max", Float, nullable=True))
    kpd_max_message_id: Optional[int] = Field(
        default=None, sa_column=Column("kpd_max_message_id", BigInteger, nullable=True)
    )

    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column("updated_at", DateTime(timezone=True), nullable=False),
    )


class ChannelEvent(SQLModel, table=True):
    """Append-only trajectory events: ``shift``, ``peak``, ``summary``.

    Same guarantee as ``message_events`` — nothing here is ever rewritten, so the
    history of a channel's trajectory survives recomputation of ``channel_states``.
    """

    __tablename__ = "channel_events"
    __table_args__ = (Index("ix_channel_events_chat_created", "chat_id", "created_at"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: int = Field(sa_column=Column("chat_id", BigInteger, nullable=False, index=True))
    kind: str = Field(sa_column=Column("kind", String(24), nullable=False))
    message_id: Optional[int] = Field(
        default=None, sa_column=Column("message_id", BigInteger, nullable=True)
    )
    note: Optional[str] = Field(default=None, sa_column=Column("note", Text, nullable=True))
    payload: Optional[dict[str, Any]] = Field(
        default=None, sa_column=Column("payload", JSON, nullable=True)
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False),
    )
