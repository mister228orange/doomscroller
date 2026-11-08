"""The scoring agent: read the archive, score unread posts one by one, keep the trajectory.

The shape of a run (this is the whole point of the service):

1. **Wake up.** Scheduled by the compose service (``score --loop``) or run once by hand.
2. **Find what is unread.** Not a second cursor: a post is unread when ``post_scores`` has
   no ``scored`` row for it. That makes the agent resumable, idempotent, and safe to run
   twice — and it means a fresh channel is backfilled from its very first stored message.
3. **Score one post at a time**, oldest first. Never a batch: every post must be judged
   against the trend *as it was before that post*.
4. **Write the score and the new channel state in the same transaction.** If the process
   dies, the post is either scored and folded into the trend, or neither.
5. **Rebuild the channel memory** every N posts: the profile line and the trajectory
   summary that the next scoring call reads in ``CHANNEL_STATE``.

Failures are first-class: a post that the model could not score gets a ``failed`` row with
the error and an attempt counter, is retried later, and never advances the trend.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Optional, Protocol

from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from ..config import ScorerConfig
from ..db import init_db, session_scope
from ..models import ChannelState, MediaOcr, Message, PostScore, Subscription, utcnow
from ..ocr import combined_body
from ..subscriptions import get_subscription, list_subscriptions, normalise_ref
from .llm import ChatClient, LLMError, LLMResponse
from .prompts import RubricModules, load_modules, render_channel_state, render_prompt
from .rubric import (
    PRIMARY_SPHERES,
    SECONDARY_TAGS,
    Anchor,
    Rubric,
    RubricError,
    extract_json,
    finalize,
    parse_anchors,
)
from .state import (
    apply_score,
    empty_snapshot,
    get_or_create_state,
    recompute_state,
    record_summary_event,
    refresh_counters,
    state_snapshot,
)

logger = logging.getLogger(__name__)

TITLE_LIMIT = 80
CHUNK = 200


class ChatBackend(Protocol):
    """What the agent needs from a model. ``ChatClient`` implements it; tests fake it."""

    async def chat(self, *, system: str, user: str) -> LLMResponse: ...


# ── report ────────────────────────────────────────────────────────────────────

@dataclass
class ChannelReport:
    chat_ref: str
    chat_id: Optional[int]
    scored: int = 0
    skipped: int = 0
    failed: int = 0
    summaries: int = 0
    #: Tokens this channel cost this run — a paid endpoint makes volume worth reporting.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: Optional[str] = None
    prompts: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out = {
            "chat": self.chat_ref,
            "chat_id": self.chat_id,
            "scored": self.scored,
            "skipped": self.skipped,
            "failed": self.failed,
            "summaries": self.summaries,
            "tokens": {
                "prompt": self.prompt_tokens,
                "completion": self.completion_tokens,
            },
        }
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class ScoreReport:
    channels: list[ChannelReport] = field(default_factory=list)
    model: Optional[str] = None
    prompt_version: Optional[str] = None
    dry_run: bool = False
    aborted: Optional[str] = None

    @property
    def scored(self) -> int:
        return sum(c.scored for c in self.channels)

    @property
    def skipped(self) -> int:
        return sum(c.skipped for c in self.channels)

    @property
    def failed(self) -> int:
        return sum(c.failed for c in self.channels)

    @property
    def prompt_tokens(self) -> int:
        return sum(c.prompt_tokens for c in self.channels)

    @property
    def completion_tokens(self) -> int:
        return sum(c.completion_tokens for c in self.channels)

    @property
    def prompts(self) -> list[dict[str, Any]]:
        return [prompt for channel in self.channels for prompt in channel.prompts]

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "scored": self.scored,
            "skipped": self.skipped,
            "failed": self.failed,
            "tokens": {"prompt": self.prompt_tokens, "completion": self.completion_tokens},
            "aborted": self.aborted,
            "channels": [c.as_dict() for c in self.channels],
        }


# ── helpers ───────────────────────────────────────────────────────────────────

def _timestamp(value: Optional[datetime]) -> Optional[float]:
    """SQLite hands back naive datetimes; compare everything as UTC epochs."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def post_title(text: str) -> str:
    """Derive an anchor title from the post itself (the rubric JSON has no title field)."""
    from ..ocr import OCR_MARKER

    for line in (text or "").strip().splitlines():
        if line.strip() == OCR_MARKER:
            continue  # the transcription marker is bookkeeping, not a title
        candidate = line.strip()
        if not candidate:
            continue
        candidate = re.sub(r"^[#>*\-—–\s]+", "", candidate)
        candidate = candidate.replace("**", "").replace("__", "").strip()
        if candidate:
            return candidate[:TITLE_LIMIT] + ("…" if len(candidate) > TITLE_LIMIT else "")
    return "(без заголовка)"


def needs_scoring(
    message: Message,
    score: Optional[PostScore],
    *,
    force: bool,
    rescore_edited: bool,
    max_attempts: int,
    ocr_updated: Optional[float] = None,
) -> bool:
    if force:
        return True
    if score is None:
        return True
    if score.status == "failed":
        return int(score.attempts or 0) < max_attempts
    seen = _timestamp(score.scored_at) or _timestamp(score.updated_at)
    # OCR text that arrived after the score changes what the rubric should have read —
    # the same trigger as an upstream edit.
    if ocr_updated is not None and (seen is None or ocr_updated > seen):
        return True
    edited = _timestamp(message.edited_at)
    if edited is None or not rescore_edited:
        return False
    return seen is None or edited > seen


def iter_pending_posts(
    session: Session,
    *,
    chat_id: int,
    force: bool,
    rescore_edited: bool = True,
    max_attempts: int = 3,
    chunk: int = CHUNK,
    attempted: Optional[set[int]] = None,
) -> Iterator[tuple[Message, Optional[PostScore], str]]:
    """Yield posts that still need a score, oldest first, streaming over the archive.

    Yields ``(message, existing_score, body)`` where ``body`` is the full text the rubric
    should see: the caption plus, for photo posts, the text OCR read off the image. A
    message with neither caption nor readable OCR text is silently skipped — it is not a
    post, and a photo that OCR already declared textless stays ignored.

    No offset and no cursor: paging by ``message_id > last`` keeps this linear even for a
    chat with hundreds of thousands of stored messages.

    ``attempted`` holds the message ids this run has already touched: one attempt per post
    per tick. A failed post is retried on the *next* tick, not immediately in a loop.
    """
    attempted = attempted if attempted is not None else set()
    after = 0
    while True:
        rows = list(
            session.exec(
                select(Message)
                .where(
                    Message.chat_id == chat_id,
                    Message.message_id > after,
                    Message.deleted == False,  # noqa: E712
                )
                .order_by(Message.message_id.asc())
                .limit(chunk)
            ).all()
        )
        if not rows:
            return

        ids = [row.message_id for row in rows]
        existing = {
            score.message_id: score
            for score in session.exec(
                select(PostScore).where(
                    PostScore.chat_id == chat_id, PostScore.message_id.in_(ids)
                )
            ).all()
        }
        ocr = {
            row.message_id: row
            for row in session.exec(
                select(MediaOcr).where(
                    MediaOcr.chat_id == chat_id,
                    MediaOcr.status == "ok",
                    MediaOcr.message_id.in_(ids),
                )
            ).all()
        }
        for message in rows:
            if message.message_id in attempted:
                continue
            row = ocr.get(message.message_id)
            body = combined_body(message.text, row.ocr_text if row else None)
            if not body.strip():
                continue
            score = existing.get(message.message_id)
            if needs_scoring(
                message,
                score,
                force=force,
                rescore_edited=rescore_edited,
                max_attempts=max_attempts,
                ocr_updated=_timestamp(row.updated_at) if row else None,
            ):
                yield message, score, body

        after = ids[-1]
        if len(rows) < chunk:
            return


def _set_axes(row: PostScore, rubric: Rubric) -> None:
    for column, value in rubric.axis_columns().items():
        setattr(row, column, value)


def _apply_rubric(row: PostScore, rubric: Rubric, *, model: str, version: str) -> PostScore:
    row.status = "scored"
    row.detail = None
    row.model = model
    row.prompt_version = version
    row.genre = rubric.genre or None
    row.primary = rubric.primary
    row.secondary = rubric.secondary
    row.main_thesis = rubric.main_thesis
    row.clauses = rubric.clauses or None
    row.density_type = rubric.density_type
    row.flags = rubric.flags
    _set_axes(row, rubric)
    row.kpd = rubric.kpd
    row.eff = rubric.eff
    row.confidence = rubric.confidence
    row.justification = rubric.justification or None
    row.nearest_anchors = rubric.nearest_anchors or None
    row.selfcheck = rubric.violations or None
    row.raw = rubric.raw
    row.scored_at = utcnow()
    row.updated_at = utcnow()
    return row


# ── persistence (one transaction per post) ────────────────────────────────────

def store_skipped(
    chat_id: int, message: Message, *, reason: str, window: int,
    body: Optional[str] = None,
) -> None:
    with session_scope() as session:
        state = get_or_create_state(session, chat_id=chat_id, window=window)
        row = session.exec(
            select(PostScore).where(
                PostScore.chat_id == chat_id, PostScore.message_id == message.message_id
            )
        ).first()
        was_scored = bool(row is not None and row.status == "scored")
        if row is None:
            row = PostScore(chat_id=chat_id, message_id=message.message_id)
        row.status = "skipped"
        row.detail = reason
        row.title = post_title(body if body is not None else (message.text or ""))
        row.message_date = message.date
        row.updated_at = utcnow()
        row.attempts = int(row.attempts or 0) + 1
        session.add(row)
        if was_scored:
            # The post was scored before and has since been edited into something too
            # short: replay the trajectory so the stale contribution is gone.
            recompute_state(session, chat_id=chat_id, window=window)
        else:
            refresh_counters(session, state)


def store_failure(
    chat_id: int,
    message: Message,
    *,
    error: str,
    model: str,
    version: str,
    window: int,
    body: Optional[str] = None,
) -> None:
    with session_scope() as session:
        state = get_or_create_state(session, chat_id=chat_id, window=window)
        row = session.exec(
            select(PostScore).where(
                PostScore.chat_id == chat_id, PostScore.message_id == message.message_id
            )
        ).first()
        if row is None:
            row = PostScore(chat_id=chat_id, message_id=message.message_id)
        previous_status = row.status
        row.status = "failed"
        row.detail = error[:1000]
        row.title = post_title(body if body is not None else (message.text or ""))
        row.message_date = message.date
        row.model = model
        row.prompt_version = version
        row.attempts = int(row.attempts or 0) + 1
        row.updated_at = utcnow()
        session.add(row)
        if previous_status == "scored":
            recompute_state(session, chat_id=chat_id, window=window)
        else:
            refresh_counters(session, state)


def store_score(
    chat_id: int,
    message: Message,
    rubric: Rubric,
    *,
    model: str,
    version: str,
    window: int,
    body: Optional[str] = None,
) -> tuple[PostScore, list[str]]:
    """Persist the rubric row and fold it into the channel state — atomically."""
    with session_scope() as session:
        state = get_or_create_state(session, chat_id=chat_id, window=window)
        row = session.exec(
            select(PostScore).where(
                PostScore.chat_id == chat_id, PostScore.message_id == message.message_id
            )
        ).first()
        was_scored = bool(row is not None and row.status == "scored")
        if row is None:
            row = PostScore(chat_id=chat_id, message_id=message.message_id)
        row.message_date = message.date
        row.title = post_title(body if body is not None else (message.text or ""))
        row.attempts = int(row.attempts or 0) + 1
        _apply_rubric(row, rubric, model=model, version=version)
        session.add(row)
        session.flush()

        if was_scored:
            # Re-scoring (usually an upstream edit) must not double-count the old value:
            # replay the whole trajectory instead of applying an increment.
            state = recompute_state(session, chat_id=chat_id, window=window)
            flags = [
                flag
                for flag in (row.flags or [])
                if str(flag).startswith("deviation:")
            ]
        else:
            flags = apply_score(session, state, row, window=window)
            refresh_counters(session, state)
        session.flush()

        moved = {
            axis: delta
            for axis, delta in (row.deviation or {}).items()
            if abs(float(delta)) > 0.0
        }
        logger.info(
            "scored %s/%s KPD=%.2f (%s%s)",
            chat_id,
            message.message_id,
            row.kpd or 0.0,
            row.primary,
            f", deviation {moved}" if moved else "",
        )
        return row, flags


# ── channel memory (profile + trajectory summary) ─────────────────────────────

def render_summary_prompt(
    modules: RubricModules,
    snapshot: dict[str, Any],
    posts: list[PostScore],
) -> tuple[str, str]:
    lines = [
        f"CHANNEL: {snapshot.get('chat_ref') or snapshot.get('chat_id')} "
        f"({snapshot.get('title') or 'без названия'})",
        f"PROFILE: {snapshot.get('profile_description') or '— (пусто)'}",
        f"SUMMARY: {snapshot.get('summary') or '— (пусто)'}",
        f"SCORED_POSTS: {snapshot.get('total_scored', 0)}",
        "RECENT_POSTS:",
    ]
    for post in posts:
        axes = " ".join(
            f"{axis} {value:.1f}"
            for axis, value in (
                ("I", post.score_i),
                ("V", post.score_v),
                ("U", post.score_u),
                ("P", post.score_p),
                ("S", post.score_s),
                ("D", post.score_d),
            )
            if value is not None
        )
        tags = "/".join([post.primary or "?"] + list(post.secondary or []))
        flags = ", ".join(post.flags or [])
        lines.append(
            f"- {post.message_id}: [{tags}] {axes} → КПД {post.kpd:.2f}; "
            f"тезис: {post.main_thesis or '—'}"
            + (f"; флаги: {flags}" if flags else "")
        )
    return modules.summary_text(), "\n".join(lines)


def apply_summary(
    chat_id: int,
    payload: dict[str, Any],
    *,
    scored_total: int,
) -> None:
    with session_scope() as session:
        state = get_or_create_state(session, chat_id=chat_id)
        profile = payload.get("profile")
        summary = payload.get("summary")
        if isinstance(profile, str) and profile.strip():
            state.profile_description = profile.strip()[:1000]
        if isinstance(summary, str) and summary.strip():
            state.summary = summary.strip()[:4000]

        primary = str(payload.get("primary_dominant") or "").strip().upper()
        if primary in PRIMARY_SPHERES:
            state.primary_dominant = primary
        secondary = payload.get("secondary_dominant") or []
        if isinstance(secondary, str):
            secondary = [part.strip().upper() for part in re.split(r"[,\s]+", secondary)]
        clean = [tag for tag in secondary if isinstance(tag, str) and tag in SECONDARY_TAGS][:3]
        if clean:
            state.secondary_dominant = clean

        drift = payload.get("drift_note")
        if isinstance(drift, str) and drift.strip() and drift.strip().lower() not in {"null", "none"}:
            state.drift_note = drift.strip()[:500]
        state.summary_at = utcnow()
        state.summary_post_count = scored_total
        state.updated_at = utcnow()
        session.add(state)
        record_summary_event(
            session,
            chat_id,
            note=(state.drift_note or "сводка обновлена")[:300],
            payload={"scored_total": scored_total},
        )


async def refresh_memory(
    chat_id: int,
    *,
    llm: ChatBackend,
    modules: RubricModules,
    limit: int = 10,
    usage: Optional[dict[str, int]] = None,
) -> Optional[dict[str, Any]]:
    """One extra call per N posts: keep the channel profile and summary current."""
    with session_scope() as session:
        state = get_or_create_state(session, chat_id=chat_id)
        snapshot = state_snapshot(session, state, context_anchors=3, events=5)
        posts = list(
            session.exec(
                select(PostScore)
                .where(PostScore.chat_id == chat_id, PostScore.status == "scored")
                .order_by(PostScore.message_id.desc())
                .limit(limit)
            ).all()
        )
    if not posts:
        return None
    posts.reverse()

    system, user = render_summary_prompt(modules, snapshot, posts)
    try:
        response = await llm.chat(system=system, user=user)
        payload = extract_json(response.text)
    except (LLMError, RubricError) as exc:
        logger.warning("channel memory refresh failed for %s: %s", chat_id, exc)
        return None
    if usage is not None:
        usage["prompt"] = usage.get("prompt", 0) + int(response.prompt_tokens or 0)
        usage["completion"] = usage.get("completion", 0) + int(response.completion_tokens or 0)

    apply_summary(chat_id, payload, scored_total=int(snapshot.get("total_scored") or 0))
    logger.info("channel memory refreshed for %s: %s", chat_id, payload.get("profile"))
    return payload


# ── the run ───────────────────────────────────────────────────────────────────

def _ordered_subscriptions(session: Session, only_chat: Optional[str]) -> list[Subscription]:
    subs = [s for s in list_subscriptions(session, enabled_only=True) if s.chat_id]
    if only_chat:
        wanted = normalise_ref(only_chat)
        sub = get_subscription(session, only_chat)
        if sub is None:
            raise SystemExit(
                f"{only_chat!r} is not subscribed — add it with `doomscroller subscribe {only_chat}`"
            )
        if sub.chat_id is None:
            raise SystemExit(
                f"{wanted!r} has no resolved chat id yet — run `doomscroller poll` first."
            )
        subs = [sub]

    # Fairness: the channel that waited longest goes first, so one huge channel cannot
    # starve the others when a run is limited.
    def last_scored_at(sub: Subscription) -> float:
        state = session.exec(
            select(ChannelState).where(ChannelState.chat_id == sub.chat_id)
        ).first()
        return _timestamp(state.last_scored_at) or 0.0 if state else 0.0

    return sorted(subs, key=last_scored_at)


async def _score_channel(
    sub: Subscription,
    *,
    config: ScorerConfig,
    modules: RubricModules,
    anchors: dict[str, Anchor],
    llm: Optional[ChatBackend],
    remaining: int,
    force: bool,
    rescore_edited: bool,
    dry_run: bool,
    summary_every: int,
) -> ChannelReport:
    report = ChannelReport(chat_ref=sub.chat_ref, chat_id=sub.chat_id)
    # The run limit bounds *model calls*, not posts examined: walking past a thousand
    # archived one-liners costs nothing and must not consume the tick's budget.
    calls = 0
    scanned = 0
    scan_cap = max(1000, remaining * 50)
    attempted: set[int] = set()

    while calls < remaining and scanned < scan_cap:
        with session_scope() as session:
            if dry_run:
                # A dry run must write nothing at all — not even a first ChannelState.
                state = session.exec(
                    select(ChannelState).where(ChannelState.chat_id == int(sub.chat_id))
                ).first()
                snapshot = (
                    state_snapshot(session, state)
                    if state is not None
                    else empty_snapshot(int(sub.chat_id), sub.chat_ref)
                )
            else:
                state = get_or_create_state(
                    session,
                    chat_id=int(sub.chat_id),
                    chat_ref=sub.chat_ref,
                    title=sub.title,
                    window=config.window,
                )
                snapshot = state_snapshot(session, state)
            pending = next(
                iter_pending_posts(
                    session,
                    chat_id=int(sub.chat_id),
                    force=force,
                    rescore_edited=rescore_edited,
                    max_attempts=config.max_attempts,
                    attempted=attempted,
                ),
                None,
            )
        if pending is None:
            break

        message, _existing, body = pending
        attempted.add(int(message.message_id))
        scanned += 1
        text = body

        if len(text.strip()) < config.min_chars:
            if dry_run:
                report.skipped += 1
                continue
            store_skipped(
                int(sub.chat_id),
                message,
                reason=f"текст короче {config.min_chars} символов ({len(text.strip())})",
                window=config.window,
                body=text,
            )
            report.skipped += 1
            continue

        prompt = render_prompt(
            modules,
            text=text,
            genre=config.genre or None,
            channel_state=render_channel_state(snapshot),
            max_chars=config.max_chars,
        )

        if dry_run:
            report.prompts.append(
                {
                    "chat": sub.chat_ref,
                    "message_id": message.message_id,
                    "system": prompt.system,
                    "user": prompt.user,
                    "prompt_version": prompt.version,
                    "channel_state": snapshot,
                }
            )
            # Nothing is stored in a dry run, so the same post would be picked again.
            break

        assert llm is not None
        calls += 1

        try:
            response = await llm.chat(system=prompt.system, user=prompt.user)
        except LLMError as exc:
            # The backend is unavailable: stop this channel, the next tick retries.
            report.failed += 1
            report.error = str(exc)
            store_failure(
                int(sub.chat_id),
                message,
                error=str(exc),
                model=config.model,
                version=prompt.version,
                window=config.window,
                body=text,
            )
            return report

        try:
            rubric = finalize(
                extract_json(response.text),
                anchors=anchors,
                genre_hint=config.genre or None,
            )
        except RubricError as exc:
            report.failed += 1
            store_failure(
                int(sub.chat_id),
                message,
                error=f"{type(exc).__name__}: {exc}",
                model=response.model,
                version=prompt.version,
                window=config.window,
                body=text,
            )
            continue

        store_score(
            int(sub.chat_id),
            message,
            rubric,
            model=response.model,
            version=prompt.version,
            window=config.window,
            body=text,
        )
        report.scored += 1
        report.prompt_tokens += int(response.prompt_tokens or 0)
        report.completion_tokens += int(response.completion_tokens or 0)
        logger.info(
            "post %s/%s: %s %s, KPD %.2f, %s tokens in / %s out",
            sub.chat_id,
            message.message_id,
            rubric.primary,
            "/".join(rubric.secondary) or "-",
            rubric.kpd,
            response.prompt_tokens,
            response.completion_tokens,
        )

        if summary_every > 0:
            with session_scope() as session:
                state = get_or_create_state(session, chat_id=int(sub.chat_id))
                pending_summary = int(state.total_scored or 0) - int(state.summary_post_count or 0)
                is_first = not state.summary
            if is_first or pending_summary >= summary_every:
                usage = {"prompt": 0, "completion": 0}
                if await refresh_memory(
                    int(sub.chat_id), llm=llm, modules=modules, usage=usage
                ):
                    report.summaries += 1
                report.prompt_tokens += usage["prompt"]
                report.completion_tokens += usage["completion"]

    return report


async def run_agent(
    config: ScorerConfig,
    *,
    only_chat: Optional[str] = None,
    limit: Optional[int] = None,
    force: bool = False,
    rescore_edited: bool = True,
    dry_run: bool = False,
    summary_every: Optional[int] = None,
    llm: Optional[ChatBackend] = None,
    modules: Optional[RubricModules] = None,
    anchors: Optional[dict[str, Anchor]] = None,
) -> ScoreReport:
    """Score every unread post in every (or one) enabled subscription, once."""
    init_db()
    modules = modules or load_modules(config.rubric_dir)
    anchors = anchors if anchors is not None else parse_anchors(modules.anchor_text)
    summary_every = config.summary_every if summary_every is None else summary_every
    remaining = config.max_posts_per_run if limit is None else max(0, int(limit))

    report = ScoreReport(model=config.model, prompt_version=modules.version, dry_run=dry_run)

    with session_scope() as session:
        subs = _ordered_subscriptions(session, only_chat)
        subs = list(subs)

    if not subs:
        report.aborted = "no enabled subscriptions with a resolved chat id"
        return report

    owned_backend = llm is None and not dry_run
    backend = llm
    if owned_backend:
        backend = ChatClient(
            base_url=config.base_url,
            model=config.model,
            api_key=config.api_key,
            temperature=config.temperature,
            timeout=config.timeout,
            retries=config.retries,
            json_mode=config.json_mode,
            max_tokens=config.max_tokens or None,
        )
        await backend.__aenter__()  # type: ignore[attr-defined]

    try:
        for sub in subs:
            if remaining <= 0:
                break
            channel_report = await _score_channel(
                sub,
                config=config,
                modules=modules,
                anchors=anchors,
                llm=backend,
                remaining=remaining,
                force=force,
                rescore_edited=rescore_edited,
                dry_run=dry_run,
                summary_every=summary_every,
            )
            report.channels.append(channel_report)
            remaining -= channel_report.scored + channel_report.failed
            if channel_report.error:
                report.aborted = channel_report.error
                break
    finally:
        if owned_backend and backend is not None:
            await backend.__aexit__(None, None, None)  # type: ignore[attr-defined]

    return report


async def run_loop(config: ScorerConfig, *, interval: Optional[int] = None, **kwargs: Any) -> None:
    """The scheduler: score, sleep, score again. This is what the compose service runs."""
    delay = max(5, int(interval if interval is not None else config.interval))
    logger.info("scorer loop: tick every %ss", delay)
    while True:
        try:
            report = await run_agent(config, **kwargs)
            logger.info("scoring tick: %s", report.as_dict())
        except LLMError as exc:
            # A missing endpoint or a dead backend: say it once per tick, keep scheduling.
            logger.error("scoring tick skipped (model backend): %s", exc)
        except OperationalError as exc:
            logger.error("scoring tick skipped (database): %s", exc)
        except Exception as exc:  # noqa: BLE001 - a bad tick must not kill the scheduler
            logger.exception("scoring tick failed: %s", exc)
        await asyncio.sleep(delay)
