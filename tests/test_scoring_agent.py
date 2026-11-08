"""The scoring agent end to end: unread posts -> rubric rows -> channel trajectory."""

from __future__ import annotations

import pytest
from sqlmodel import select

from doomscroller.config import ScorerConfig
from doomscroller.models import ChannelEvent, ChannelState, Message, PostScore
from doomscroller.scoring.agent import (
    needs_scoring,
    post_title,
    run_agent,
    store_score,
    iter_pending_posts,
)
from doomscroller.scoring.llm import LLMError
from doomscroller.scoring.prompts import load_modules
from doomscroller.scoring.rubric import finalize, parse_anchors
from doomscroller.store import record_event
from doomscroller.subscriptions import subscribe

from fakes import FakeScorerLLM, score_answer, summary_answer

CHAT = -1007770001111
LONG = (
    "Плотность — это несущие тезисы на клаузу, а не объём текста. Первый тезис: "
    "форма задаёт содержание, и абзац из пяти связок не становится аргументом. "
    "Второй тезис: инструмент отличается от информации тем, что его можно применить "
    "к другому случаю и проверить. Третий тезис: закрытая плотность повышает D и "
    "понижает P, и это не пароль — тезис восстанавливается при знании контекста."
)


def _seed(session, *, count: int = 3, text: str = LONG) -> int:
    sub = subscribe(session, "testchat")
    sub.chat_id = CHAT
    sub.title = "Test Channel"
    for index in range(1, count + 1):
        record_event(
            session,
            chat_id=CHAT,
            message_id=index,
            event_type="new",
            text=f"{text} (пост {index})",
            sender_name="author",
        )
    session.commit()
    return CHAT


def _config(**overrides) -> ScorerConfig:
    base = dict(
        rubric_dir=__import__("doomscroller.scoring.prompts", fromlist=["x"]).default_rubric_dir(),
        model="fake-model",
        base_url="http://unused",
        min_chars=40,
        max_chars=5000,
        max_posts_per_run=50,
        summary_every=0,
        window=5,
    )
    base.update(overrides)
    return ScorerConfig(**base)


def _rows(session) -> list[PostScore]:
    return list(session.exec(select(PostScore).order_by(PostScore.message_id)).all())


def _state(session) -> ChannelState | None:
    return session.exec(select(ChannelState).where(ChannelState.chat_id == CHAT)).first()


# ── the happy path ────────────────────────────────────────────────────────────

async def test_scores_every_unread_post_one_by_one(session):
    _seed(session, count=3)
    llm = FakeScorerLLM([])
    report = await run_agent(_config(), llm=llm)

    assert report.scored == 3
    assert report.failed == 0
    assert report.skipped == 0
    rows = _rows(session)
    assert [row.status for row in rows] == ["scored"] * 3
    assert [row.message_id for row in rows] == [1, 2, 3]
    assert rows[0].kpd == 7.62  # (8 + 7.5 + 7 + 8) / 4
    assert rows[0].eff == 1.52  # 7.62 / max(P, 1)
    assert rows[0].primary == "SCI"
    assert rows[0].secondary == ["TRUST"]
    assert rows[0].prompt_version
    assert rows[0].model == "fake-model"


async def test_the_second_run_has_nothing_to_do(session):
    _seed(session, count=2)
    first = await run_agent(_config(), llm=FakeScorerLLM([]))
    assert first.scored == 2

    llm = FakeScorerLLM([])
    second = await run_agent(_config(), llm=llm)
    assert second.scored == 0
    assert llm.prompts == [], "a fully scored channel must not call the model again"


async def test_backfill_starts_from_the_oldest_message(session):
    _seed(session, count=4)
    llm = FakeScorerLLM([])
    await run_agent(_config(), llm=llm)
    # one call per post, oldest first — this is the "read all unread from the start" rule
    order = [row.message_id for row in _rows(session)]
    assert order == [1, 2, 3, 4]


async def test_the_trajectory_is_fed_into_the_next_prompt(session):
    _seed(session, count=2)
    llm = FakeScorerLLM([])
    await run_agent(_config(), llm=llm)

    first_prompt = llm.prompts[0][1]
    second_prompt = llm.prompts[1][1]
    assert "первый пост канала" in first_prompt
    assert "TREND:" in second_prompt
    assert "I 8.0" in second_prompt  # the first post's scores became the trend
    assert "LOCAL_ANCHORS:" in second_prompt
    assert "p_1" in second_prompt


async def test_channel_state_is_built_after_the_run(session):
    _seed(session, count=3)
    await run_agent(_config(), llm=FakeScorerLLM([]))
    state = _state(session)
    assert state is not None
    assert state.total_scored == 3
    assert state.last_scored_message_id == 3
    assert state.means["I"] == pytest.approx(8.0)
    assert state.tag_distribution == {"SCI": 3}
    assert len(state.local_anchors) == 3
    assert state.last_scored_at is not None


async def test_the_run_reports_the_paid_volume(session):
    """A paid endpoint makes token volume part of the run result."""
    _seed(session, count=2)
    report = await run_agent(_config(), llm=FakeScorerLLM([]))
    assert report.scored == 2
    assert report.prompt_tokens == 2000
    assert report.completion_tokens == 200
    assert report.as_dict()["tokens"] == {"prompt": 2000, "completion": 200}
    channel = report.channels[0].as_dict()
    assert channel["tokens"] == {"prompt": 2000, "completion": 200}


async def test_the_memory_call_volume_is_counted_too(session):
    _seed(session, count=1)
    report = await run_agent(_config(summary_every=5), llm=FakeScorerLLM([]))
    assert report.scored == 1
    # the score call plus the channel-memory call
    assert report.prompt_tokens == 2000
    assert report.completion_tokens == 200


# ── skipping and failing ──────────────────────────────────────────────────────

async def test_short_posts_are_recorded_as_skipped_not_scored(session):
    _seed(session, count=1, text="коротко")
    llm = FakeScorerLLM([])
    report = await run_agent(_config(), llm=llm)

    assert report.scored == 0
    assert report.skipped == 1
    assert llm.prompts == []
    rows = _rows(session)
    assert rows[0].status == "skipped"
    assert "короче" in (rows[0].detail or "")
    state = _state(session)
    assert state.total_scored == 0
    assert state.total_skipped == 1


async def test_empty_text_messages_are_not_candidates(session):
    sub = subscribe(session, "testchat")
    sub.chat_id = CHAT
    record_event(session, chat_id=CHAT, message_id=1, event_type="new", text=None)
    record_event(session, chat_id=CHAT, message_id=2, event_type="new", text=LONG)
    session.commit()

    llm = FakeScorerLLM([])
    report = await run_agent(_config(), llm=llm)
    assert report.scored == 1
    assert [row.message_id for row in _rows(session)] == [2]


async def test_a_deleted_message_is_not_scored(session):
    sub = subscribe(session, "testchat")
    sub.chat_id = CHAT
    record_event(session, chat_id=CHAT, message_id=1, event_type="new", text=LONG)
    record_event(session, chat_id=CHAT, message_id=1, event_type="deleted")
    record_event(session, chat_id=CHAT, message_id=2, event_type="new", text=LONG)
    session.commit()

    report = await run_agent(_config(), llm=FakeScorerLLM([]))
    assert report.scored == 1
    assert [row.message_id for row in _rows(session)] == [2]


async def test_a_bad_answer_fails_the_post_without_stopping_the_channel(session):
    _seed(session, count=2)
    llm = FakeScorerLLM(["совсем не JSON", score_answer()])
    report = await run_agent(_config(), llm=llm)

    assert report.scored == 1
    assert report.failed == 1
    rows = _rows(session)
    assert [row.status for row in rows] == ["failed", "scored"]
    assert "no JSON object" in (rows[0].detail or "")
    assert rows[0].attempts == 1
    # the trend advanced only for the post that was actually scored
    assert _state(session).total_scored == 1


async def test_an_unreachable_backend_aborts_the_tick_but_keeps_the_attempt(session):
    _seed(session, count=2)
    llm = FakeScorerLLM([LLMError("connection refused")])
    report = await run_agent(_config(), llm=llm)

    assert report.failed == 1
    assert report.scored == 0
    assert "connection refused" in (report.aborted or "")
    rows = _rows(session)
    assert len(rows) == 1 and rows[0].status == "failed"
    assert rows[0].attempts == 1


async def test_failed_posts_are_retried_then_given_up(session):
    _seed(session, count=1)
    for _ in range(2):
        await run_agent(_config(max_attempts=2), llm=FakeScorerLLM([LLMError("down")]))

    row = _rows(session)[0]
    assert row.status == "failed"
    assert row.attempts == 2

    llm = FakeScorerLLM([])
    report = await run_agent(_config(max_attempts=2), llm=llm)
    assert report.scored == 0, "a post that used up its attempts is not retried"
    assert llm.prompts == []


async def test_retry_succeeds_when_the_backend_comes_back(session):
    _seed(session, count=1)
    await run_agent(_config(max_attempts=3), llm=FakeScorerLLM([LLMError("down")]))
    assert _rows(session)[0].status == "failed"

    report = await run_agent(_config(max_attempts=3), llm=FakeScorerLLM([]))
    assert report.scored == 1
    row = _rows(session)[0]
    assert row.status == "scored"
    assert row.attempts == 2


async def test_counters_stay_consistent_when_a_failed_post_succeeds(session):
    _seed(session, count=1)
    await run_agent(_config(), llm=FakeScorerLLM([LLMError("down")]))
    assert _state(session).total_failed == 1
    assert _state(session).total_scored == 0

    await run_agent(_config(), llm=FakeScorerLLM([]))
    state = _state(session)
    assert state.total_failed == 0
    assert state.total_scored == 1
    assert state.total_skipped == 0


# ── edits, re-scoring, limits ────────────────────────────────────────────────

async def test_an_edited_post_is_re_scored_and_the_trend_rebuilt(session):
    _seed(session, count=1)
    await run_agent(_config(), llm=FakeScorerLLM([score_answer(i=8.0)]))
    assert _state(session).means["I"] == 8.0

    # the author edits the post upstream, after it was scored
    record_event(
        session, chat_id=CHAT, message_id=1, event_type="edited", text=LONG + " правка"
    )
    session.commit()

    llm = FakeScorerLLM([score_answer(i=3.0)])
    report = await run_agent(_config(), llm=llm)
    assert report.scored == 1
    assert "TREND:" in llm.prompts[0][1] or "TREND:" in llm.prompts[0][0]

    row = _rows(session)[0]
    assert row.score_i == 3.0
    assert row.attempts == 2
    # replayed, not double-counted
    assert _state(session).total_scored == 1
    assert _state(session).means["I"] == 3.0


async def test_force_rescores_everything(session):
    _seed(session, count=2)
    await run_agent(_config(), llm=FakeScorerLLM([]))
    llm = FakeScorerLLM([])
    report = await run_agent(_config(), force=True, llm=llm)
    assert report.scored == 2
    assert len(_rows(session)) == 2  # updated in place, not duplicated
    assert _state(session).total_scored == 2


async def test_the_run_limit_spans_channels(session):
    _seed(session, count=3)
    report = await run_agent(_config(), limit=2, llm=FakeScorerLLM([]))
    assert report.scored == 2
    assert [row.message_id for row in _rows(session)] == [1, 2]

    # the next tick picks up where this one stopped
    report = await run_agent(_config(), limit=2, llm=FakeScorerLLM([]))
    assert [row.message_id for row in _rows(session)] == [1, 2, 3]


async def test_short_posts_do_not_consume_the_run_limit(session):
    sub = subscribe(session, "testchat")
    sub.chat_id = CHAT
    for index in range(1, 6):
        record_event(session, chat_id=CHAT, message_id=index, event_type="new", text="ок")
    record_event(session, chat_id=CHAT, message_id=6, event_type="new", text=LONG)
    session.commit()

    llm = FakeScorerLLM([])
    report = await run_agent(_config(), limit=1, llm=llm)
    assert report.skipped == 5
    assert report.scored == 1
    assert len(llm.prompts) == 1
    assert _rows(session)[-1].message_id == 6


async def test_no_enabled_subscriptions_is_reported(session):
    report = await run_agent(_config(), llm=FakeScorerLLM([]))
    assert report.scored == 0
    assert report.aborted == "no enabled subscriptions with a resolved chat id"


async def test_dry_run_builds_the_prompt_and_writes_nothing(session):
    _seed(session, count=2)
    llm = FakeScorerLLM([])
    report = await run_agent(_config(), llm=llm, dry_run=True)

    assert report.dry_run is True
    assert llm.prompts == []
    assert len(report.prompts) == 1
    prompt = report.prompts[0]
    assert prompt["message_id"] == 1
    assert "# ДАННЫЕ" in prompt["user"]
    assert prompt["prompt_version"]
    assert _rows(session) == []
    assert _state(session) is None


# ── channel memory ───────────────────────────────────────────────────────────

async def test_channel_memory_is_refreshed_on_the_first_post(session):
    _seed(session, count=1)
    llm = FakeScorerLLM([score_answer(), summary_answer()])
    report = await run_agent(_config(summary_every=5), llm=llm)

    assert report.scored == 1
    assert report.channels[0].summaries == 1
    state = _state(session)
    assert state.profile_description == "Канал о методе и языке."
    assert state.summary == "темы: метод; мало данных"
    assert state.primary_dominant == "SCI"
    assert state.summary_post_count == 1
    events = session.exec(select(ChannelEvent).where(ChannelEvent.kind == "summary")).all()
    assert len(events) == 1


async def test_channel_memory_respects_the_interval(session):
    _seed(session, count=4)
    llm = FakeScorerLLM([])
    llm.default = score_answer()
    # 4 posts, one memory refresh on the first post, then one every 2 posts
    llm.answers = [score_answer(), summary_answer(summary="первая сводка")]
    report = await run_agent(_config(summary_every=2), llm=llm)
    assert report.scored == 4
    # calls: score1, summary1, score2, score3, summary2, score4
    assert len(llm.prompts) == 6
    state = _state(session)
    assert state.summary_post_count == 3
    assert state.summary == "темы: метод; мало данных"


async def test_a_failed_memory_call_does_not_fail_the_run(session):
    _seed(session, count=1)
    llm = FakeScorerLLM([score_answer(), LLMError("summary down")])
    report = await run_agent(_config(summary_every=5), llm=llm)
    assert report.scored == 1
    assert report.failed == 0
    assert report.channels[0].summaries == 0
    assert _state(session).total_scored == 1


# ── small units ─────────────────────────────────────────────────────────────

def test_post_title_is_derived_from_the_first_line():
    assert post_title("# Заголовок\n\nтекст") == "Заголовок"
    assert post_title("первая строка\nвторая") == "первая строка"
    assert post_title("") == "(без заголовка)"
    assert len(post_title("я" * 200)) == 81  # 80 chars + ellipsis


def test_needs_scoring_matrix():
    row = PostScore(chat_id=1, message_id=1, status="scored", attempts=1)
    message = Message(chat_id=1, message_id=1)
    assert needs_scoring(message, None, force=False, rescore_edited=True, max_attempts=3)
    assert not needs_scoring(message, row, force=False, rescore_edited=True, max_attempts=3)
    assert needs_scoring(message, row, force=True, rescore_edited=True, max_attempts=3)

    exhausted = PostScore(chat_id=1, message_id=1, status="failed", attempts=3)
    assert not needs_scoring(message, exhausted, force=False, rescore_edited=True, max_attempts=3)
    retryable = PostScore(chat_id=1, message_id=1, status="failed", attempts=1)
    assert needs_scoring(message, retryable, force=False, rescore_edited=True, max_attempts=3)


def test_sampling_reads_every_nth_post_only(session):
    """1-из-5: новые кандидаты — только message_id % 5 == 0; уже оценённые — всегда."""
    _seed(session, count=10)
    pending = list(
        iter_pending_posts(
            session, chat_id=CHAT, force=False, rescore_edited=False, sample_every=5
        )
    )
    ids = [item[0].message_id for item in pending]
    assert ids == [5, 10], ids


def test_sampling_never_drops_a_post_that_already_has_a_row(session):
    """Rescore (edit/OCR) is not subject to sampling — only first-time reads are."""
    _seed(session, count=10)
    llm = FakeScorerLLM([])
    modules = load_modules()
    anchors = parse_anchors(load_modules().anchor_text)

    from doomscroller.scoring.rubric import extract_json, finalize

    # Score post 3 with sampling off: it now carries a score row while sitting outside
    # the 1-of-5 sample.
    message, _score, body = next(
        iter(
            iter_pending_posts(
                session, chat_id=CHAT, force=True, rescore_edited=False, sample_every=5
            )
        )
    )
    assert message.message_id == 1
    rubric = finalize(extract_json(llm.default), anchors=anchors, genre_hint=None)
    store_score(CHAT, message, rubric, model="m", version=modules.version, window=5, body=body)

    # An OCR row newer than that score pulls post 3 back into the queue — sampling or not.
    from datetime import timedelta

    from doomscroller.models import MediaOcr, utcnow

    session.add(
        MediaOcr(
            chat_id=CHAT, message_id=1, status="ok", ocr_text="дописанный текст со скриншота",
            updated_at=utcnow() + timedelta(seconds=10),
        )
    )
    session.commit()
    pending = list(
        iter_pending_posts(
            session, chat_id=CHAT, force=False, rescore_edited=False, sample_every=5
        )
    )
    ids = [item[0].message_id for item in pending]
    assert 1 in ids and 5 in ids and 10 in ids, ids
    # and a never-scored post outside the sample stays out even with OCR text
    session.add(
        MediaOcr(
            chat_id=CHAT, message_id=3, status="ok", ocr_text="текст без оценки",
            updated_at=utcnow() + timedelta(seconds=10),
        )
    )
    session.commit()
    pending = list(
        iter_pending_posts(
            session, chat_id=CHAT, force=False, rescore_edited=False, sample_every=5
        )
    )
    assert [item[0].message_id for item in pending] == [1, 5, 10]


def test_force_bypasses_sampling(session):
    _seed(session, count=10)
    pending = list(
        iter_pending_posts(
            session, chat_id=CHAT, force=True, rescore_edited=False, sample_every=5
        )
    )
    assert len(pending) == 10
