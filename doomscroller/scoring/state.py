"""The trajectory of a channel: moving means, deviations, shifts, rolling memory.

This is the ``state/{{channel}}.json`` of the methodology, kept in PostgreSQL (see
``ChannelState``) so the scorer service and every reader share one copy, and the full
history stays in ``post_scores``.

Two rules shape this module:

* **Derived, not authoritative.** ``ChannelState`` is a projection of ``post_scores``.
  :func:`recompute_state` can rebuild it from scratch at any time; nothing is lost if a
  state row is wrong, which is what makes re-scoring an edited post safe.
* **Never pull a post towards the mean.** The state is used for *comparison* (deviation
  flags), never as a target. A post can be far below the channel's average and that is a
  fact about the post, not an error to correct.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import func, or_
from sqlmodel import Session, select

from ..models import ChannelEvent, ChannelState, Message, PostScore, utcnow
from .rubric import AXES

DEFAULT_WINDOW = 5
ANCHOR_LIMIT = 5
CONTEXT_ANCHORS = 3

#: |score - mean| above this is reported as a deviation (08, rule 3).
DEVIATION_FLAG = 1.0
#: |score - mean| at or above this counts towards a shift (08, rule 4).
SHIFT_DELTA = 1.5
#: How many axes must move strongly for a post to be a shift candidate.
SHIFT_AXES = 2
#: Consecutive candidates needed before a shift is recorded as an event.
SHIFT_STREAK = 3
#: A trend is not a trend until it exists. With an EWMA window of 5, the first posts compare
#: against a mean built from one or two scores, so any strong post looks like a shift. Shift
#: detection only starts once this many posts have been scored in the channel.
MIN_POSTS_FOR_SHIFT = 10
#: A new channel best is only an event once it clears this.
PEAK_MIN_KPD = 7.5
#: |KPD - KPD_mean| inside this band is "at" the trend.
TREND_BAND = 0.25


@dataclass
class AnchorView:
    id: str
    message_id: int
    title: str
    primary: str
    scores: dict[str, float]
    kpd: Optional[float]
    eff: Optional[float]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "message_id": self.message_id,
            "title": self.title,
            "primary": self.primary,
            "scores": self.scores,
            "kpd": self.kpd,
            "eff": self.eff,
        }


def get_or_create_state(
    session: Session,
    *,
    chat_id: int,
    chat_ref: Optional[str] = None,
    title: Optional[str] = None,
    window: int = DEFAULT_WINDOW,
) -> ChannelState:
    state = session.exec(select(ChannelState).where(ChannelState.chat_id == chat_id)).first()
    if state is None:
        state = ChannelState(chat_id=chat_id, chat_ref=chat_ref, title=title, window=window)
        session.add(state)
        session.flush()
        return state
    if chat_ref and state.chat_ref != chat_ref:
        state.chat_ref = chat_ref
    if title and not state.title:
        state.title = title
    return state


def _copy_means(state: ChannelState) -> dict[str, float]:
    return {axis: float(value) for axis, value in (state.means or {}).items() if axis in AXES}


def _axis_values(score: PostScore) -> dict[str, Optional[float]]:
    return {axis: getattr(score, f"score_{axis.lower()}") for axis in AXES}


def _ewma(previous: Optional[float], value: Optional[float], window: int) -> Optional[float]:
    if value is None:
        return previous
    if previous is None:
        return round(value, 3)
    return round((previous * (window - 1) + value) / window, 3)


def apply_score(
    session: Session,
    state: ChannelState,
    score: PostScore,
    *,
    window: Optional[int] = None,
    collect_events: bool = True,
) -> list[str]:
    """Fold one scored post into the channel trajectory. Returns deviation flags.

    Order matters and mirrors the methodology: deviations are measured against the trend
    *before* this post, the means are updated after, and only then is the post's position
    relative to the new trend recorded.
    """
    window = int(window or state.window or DEFAULT_WINDOW)
    window = max(2, window)

    values = _axis_values(score)
    means_before = _copy_means(state)

    flags: list[str] = []
    deviations: dict[str, float] = {}
    strong_axes: list[str] = []
    if means_before:
        for axis in AXES:
            value = values[axis]
            if value is None:
                continue
            delta = round(float(value) - means_before[axis], 2)
            deviations[axis] = delta
            if abs(delta) > DEVIATION_FLAG:
                flags.append(f"deviation:{axis}:{delta:+.1f}")
            if abs(delta) >= SHIFT_DELTA:
                strong_axes.append(axis)
    else:
        deviations = {axis: 0.0 for axis in AXES if values[axis] is not None}

    # Moving means — one EWMA per axis with the configured window.
    if means_before:
        state.means = {
            axis: _ewma(means_before.get(axis), values[axis], window) for axis in AXES
        }
    else:
        state.means = {
            axis: round(float(values[axis]), 3) for axis in AXES if values[axis] is not None
        }
    state.window = window

    # Position relative to the *new* trend (08, the `deltas` of the state file).
    new_means = state.means or {}
    state.deltas = {
        axis: round(float(values[axis]) - float(new_means[axis]), 2)
        for axis in AXES
        if values[axis] is not None and axis in new_means
    }

    state.kpd_mean = _ewma(state.kpd_mean, score.kpd, window)
    if score.eff is not None:
        state.eff_mean = _ewma(state.eff_mean, score.eff, window)
        state.eff_n = int(state.eff_n or 0) + 1

    # The score row carries its own channel-relative view, so a single post is readable
    # without replaying the whole trajectory.
    score.deviation = deviations
    score.flags = [
        flag for flag in (score.flags or []) if not str(flag).startswith("deviation:")
    ] + flags
    if score.kpd is not None and state.kpd_mean is not None:
        distance = score.kpd - state.kpd_mean
        score.trend_position = (
            "above" if distance > TREND_BAND else "below" if distance < -TREND_BAND else "at"
        )
    else:
        score.trend_position = "at"

    # Counters and the tag distribution.
    state.total_scored = int(state.total_scored or 0) + 1
    state.last_scored_message_id = score.message_id
    state.last_scored_at = score.scored_at or utcnow()

    distribution = dict(state.tag_distribution or {})
    if score.primary:
        distribution[score.primary] = int(distribution.get(score.primary, 0)) + 1
    state.tag_distribution = distribution

    # Local anchors: the last few posts of *this* channel, which the next score compares
    # against (08, rule 2).
    anchors = list(state.local_anchors or [])
    anchors.append(
        AnchorView(
            id=f"p_{score.message_id}",
            message_id=int(score.message_id),
            title=(score.title or "")[:80],
            primary=score.primary or "",
            scores={axis: float(values[axis]) for axis in AXES if values[axis] is not None},
            kpd=score.kpd,
            eff=score.eff,
        ).as_dict()
    )
    state.local_anchors = anchors[-ANCHOR_LIMIT:]

    # Peak of the channel.
    is_peak = score.kpd is not None and (
        state.kpd_max is None or float(score.kpd) > float(state.kpd_max)
    )
    if is_peak:
        state.kpd_max = score.kpd
        state.kpd_max_message_id = score.message_id
        if collect_events and float(score.kpd or 0) >= PEAK_MIN_KPD:
            _add_event(
                session,
                state.chat_id,
                kind="peak",
                message_id=score.message_id,
                note=f"новый максимум канала: КПД {float(score.kpd):.2f}",
                payload={"kpd": score.kpd},
            )

    # Shift detection: a run of posts that all move strongly on two or more axes. Gated on
    # MIN_POSTS_FOR_SHIFT: early posts would compare against a one-score "trend" and every
    # strong post would look like a shift.
    candidate = len(strong_axes) >= SHIFT_AXES and int(state.total_scored) >= MIN_POSTS_FOR_SHIFT
    score.shift_candidate = candidate
    if candidate:
        state.deviation_streak = int(state.deviation_streak or 0) + 1
        state.shift_candidate_message_id = score.message_id
        if state.deviation_streak >= SHIFT_STREAK and collect_events:
            _add_event(
                session,
                state.chat_id,
                kind="shift",
                message_id=score.message_id,
                note=(
                    f"сдвиг траектории: {state.deviation_streak} поста подряд "
                    f"сильно ушли от тренда (оси: {', '.join(sorted(set(strong_axes)))}; "
                    f"последний — {score.primary or '?'})"
                ),
                payload={"axes": sorted(set(strong_axes)), "streak": state.deviation_streak},
            )
            state.deviation_streak = 0
            state.shift_candidate_message_id = None
    else:
        state.deviation_streak = 0
        state.shift_candidate_message_id = None

    state.updated_at = utcnow()
    session.add(state)
    session.add(score)
    return flags


def _add_event(
    session: Session,
    chat_id: int,
    *,
    kind: str,
    message_id: Optional[int] = None,
    note: str = "",
    payload: Optional[dict[str, Any]] = None,
) -> ChannelEvent:
    event = ChannelEvent(
        chat_id=chat_id, kind=kind, message_id=message_id, note=note, payload=payload
    )
    session.add(event)
    return event


def record_summary_event(
    session: Session, chat_id: int, *, note: str, payload: Optional[dict[str, Any]] = None
) -> ChannelEvent:
    return _add_event(session, chat_id, kind="summary", note=note, payload=payload)


def refresh_counters(session: Session, state: ChannelState) -> None:
    """Re-count the three statuses from ``post_scores``.

    Incrementing counters by hand drifts as soon as a post changes status (skipped ->
    failed, failed -> scored after an edit). One count per status per write is cheap next
    to a model call, and it keeps ``total_scored``/``total_skipped``/``total_failed``
    exactly consistent with the rows they summarise.
    """
    session.flush()
    for status, attribute in (
        ("scored", "total_scored"),
        ("skipped", "total_skipped"),
        ("failed", "total_failed"),
    ):
        count = session.exec(
            select(func.count())
            .select_from(PostScore)
            .where(PostScore.chat_id == int(state.chat_id), PostScore.status == status)
        ).one()
        setattr(state, attribute, int(count or 0))
    state.updated_at = utcnow()
    session.add(state)


def recompute_state(
    session: Session, *, chat_id: int, window: int = DEFAULT_WINDOW
) -> ChannelState:
    """Replay every scored post to rebuild the derived projection.

    Used when a post is re-scored (it was edited upstream): incremental arithmetic would
    keep the stale contribution. Trajectory events are append-only history and are *not*
    re-emitted here.
    """
    state = get_or_create_state(session, chat_id=chat_id, window=window)
    state.means = None
    state.deltas = None
    state.kpd_mean = None
    state.eff_mean = None
    state.eff_n = 0
    state.total_scored = 0
    state.total_skipped = 0
    state.total_failed = 0
    state.tag_distribution = None
    state.local_anchors = None
    state.deviation_streak = 0
    state.shift_candidate_message_id = None
    state.kpd_max = None
    state.kpd_max_message_id = None
    state.last_scored_message_id = None
    state.last_scored_at = None

    rows = session.exec(
        select(PostScore)
        .where(PostScore.chat_id == chat_id, PostScore.status == "scored")
        .order_by(PostScore.message_id.asc())
    ).all()
    for row in rows:
        apply_score(session, state, row, window=window, collect_events=False)

    refresh_counters(session, state)
    return state


def pending_counts(session: Session, chat_ids: Optional[list[int]] = None) -> dict[int, int]:
    """How many stored posts still need a score, per chat.

    This is the same definition the agent uses ("unread" = no usable ``post_scores`` row),
    which is why it belongs here and not in the MCP layer: a reader on another machine must
    be able to tell coverage from the database alone, without the scorer's configuration.
    """
    stmt = (
        select(Message.chat_id, func.count())
        .join(
            PostScore,
            (PostScore.chat_id == Message.chat_id)
            & (PostScore.message_id == Message.message_id),
            isouter=True,
        )
        .where(
            Message.deleted == False,  # noqa: E712
            Message.text.is_not(None),
            Message.text != "",
            or_(PostScore.id.is_(None), PostScore.status == "failed"),
        )
        .group_by(Message.chat_id)
    )
    if chat_ids:
        stmt = stmt.where(Message.chat_id.in_(chat_ids))
    return {int(chat_id): int(count) for chat_id, count in session.exec(stmt).all()}


def recent_events(session: Session, chat_id: int, limit: int = 5) -> list[ChannelEvent]:
    return list(
        session.exec(
            select(ChannelEvent)
            .where(ChannelEvent.chat_id == chat_id)
            .order_by(ChannelEvent.id.desc())
            .limit(limit)
        ).all()
    )


def state_snapshot(
    session: Session,
    state: ChannelState,
    *,
    context_anchors: int = CONTEXT_ANCHORS,
    events: int = 5,
    pending: Optional[int] = None,
) -> dict[str, Any]:
    """A plain dict of the state, safe to hold while an LLM call is in flight.

    ORM objects are deliberately *not* carried across the await: the snapshot is what the
    prompt is built from, so it must be immune to a session closing or expiring.
    """
    anchors = list(state.local_anchors or [])[-max(1, context_anchors):]
    return {
        "chat_id": int(state.chat_id),
        "chat_ref": state.chat_ref,
        "title": state.title,
        "profile_description": state.profile_description,
        "summary": state.summary,
        "drift_note": state.drift_note,
        "summary_post_count": int(state.summary_post_count or 0),
        "window": int(state.window or DEFAULT_WINDOW),
        "means": dict(state.means or {}),
        "deltas": dict(state.deltas or {}),
        "kpd_mean": state.kpd_mean,
        "eff_mean": state.eff_mean,
        "total_scored": int(state.total_scored or 0),
        "total_skipped": int(state.total_skipped or 0),
        "total_failed": int(state.total_failed or 0),
        "tag_distribution": dict(state.tag_distribution or {}),
        "local_anchors": anchors,
        "deviation_streak": int(state.deviation_streak or 0),
        "kpd_max": state.kpd_max,
        "kpd_max_message_id": state.kpd_max_message_id,
        "summary_pending": int(state.total_scored or 0) - int(state.summary_post_count or 0),
        # `pending` needs its own query, so callers pass it in; None means "not computed".
        "pending": pending,
        "events": [
            {
                "date": event.created_at.strftime("%Y-%m-%d") if event.created_at else "",
                "kind": event.kind,
                "note": event.note or "",
            }
            for event in reversed(recent_events(session, state.chat_id, limit=events))
        ],
    }


def empty_snapshot(chat_id: int, chat_ref: Optional[str] = None) -> dict[str, Any]:
    """The snapshot for a channel that has never been scored."""
    return {
        "chat_id": chat_id,
        "chat_ref": chat_ref,
        "title": None,
        "profile_description": None,
        "summary": None,
        "drift_note": None,
        "summary_post_count": 0,
        "window": DEFAULT_WINDOW,
        "means": {},
        "deltas": {},
        "kpd_mean": None,
        "eff_mean": None,
        "total_scored": 0,
        "total_skipped": 0,
        "total_failed": 0,
        "tag_distribution": {},
        "local_anchors": [],
        "deviation_streak": 0,
        "kpd_max": None,
        "kpd_max_message_id": None,
        "summary_pending": 0,
        "pending": None,
        "events": [],
    }
