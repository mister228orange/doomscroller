"""Anchors as a test set: locate the reference texts, re-score them, diff the result.

The point of the locators is that "the scores we agreed on" stop being prose. These tests
pin the mechanics with a fake backend; the real model is only ever needed to run the
calibration itself.
"""

from __future__ import annotations

import pytest
from sqlmodel import select

from doomscroller.config import ScorerConfig
from doomscroller.models import ChannelState, PostScore
from doomscroller.scoring.calibrate import (
    TOLERANCE,
    axis_summary,
    calibrate,
    locate,
)
from doomscroller.scoring.llm import LLMError
from doomscroller.scoring.prompts import default_rubric_dir, load_modules
from doomscroller.scoring.rubric import parse_anchors
from doomscroller.store import record_event
from doomscroller.subscriptions import subscribe

from fakes import FakeScorerLLM, score_answer

# chat ids used by the real locators: design4TOR "Обозначающий"
CHAT_ID = 3473978404
LOCATED = {  # anchor -> message id in that channel
    "A1": 44,
    "A2": 72,
    "A3": 99,
    "A4": 98,
    "A7": 100,
}


@pytest.fixture(scope="module")
def anchors():
    return parse_anchors(load_modules().anchor_text)


def _seed_channel(session, *, skip: tuple[int, ...] = ()) -> None:
    sub = subscribe(session, "design4TOR")
    sub.chat_id = CHAT_ID
    sub.title = "Обозначающий"
    for anchor_id, message_id in LOCATED.items():
        if message_id in skip:
            continue
        record_event(
            session,
            chat_id=CHAT_ID,
            message_id=message_id,
            event_type="new",
            text=f"Текст якоря {anchor_id}. " + "Несущий тезис. " * 30,
        )
    session.commit()


def _answers_matching(anchors, *, shift: dict[str, float] | None = None) -> list[str]:
    """Answers that reproduce the reference scores, with an optional per-axis shift."""
    shift = shift or {}
    order = [a for a in sorted(anchors, key=lambda v: int(v.lstrip("A"))) if a in LOCATED]
    out = []
    for anchor_id in order:
        scores = anchors[anchor_id].scores
        out.append(
            score_answer(
                i=scores["I"] + shift.get("I", 0.0),
                v=scores["V"] + shift.get("V", 0.0),
                u=scores["U"] + shift.get("U", 0.0),
                p=scores["P"] + shift.get("P", 0.0),
                s=scores["S"] + shift.get("S", 0.0),
                d=scores["D"] + shift.get("D", 0.0),
                primary=anchors[anchor_id].primary,
                genre=anchors[anchor_id].genre,
                nearest_anchors=[],
            )
        )
    return out


# ── locators ────────────────────────────────────────────────────────────────

def test_the_rubric_file_carries_locators_for_the_found_anchors(anchors):
    found = {aid: a.message_id for aid, a in anchors.items() if a.channel}
    assert found == LOCATED
    assert all(anchors[aid].channel == "design4TOR" for aid in LOCATED)


def test_the_other_anchors_stay_prose(anchors):
    for anchor_id in ("A5", "A6", "A8", "A9", "A10"):
        assert anchors[anchor_id].channel is None
        assert anchors[anchor_id].message_id is None


def test_locate_marks_what_is_actually_archived(session, anchors):
    _seed_channel(session, skip=(44,))
    checks = {c.anchor_id: c for c in locate(session, anchors)}

    assert checks["A3"].archived is True
    assert checks["A3"].chars > 100
    assert checks["A3"].chat_id == CHAT_ID
    assert checks["A1"].archived is False
    assert "не найдено" in (checks["A1"].note or "")
    assert checks["A5"].archived is False
    assert "LOCATOR" in (checks["A5"].note or "")


def test_locate_handles_a_channel_that_is_not_subscribed(session, anchors):
    subscribe(session, "someotherchannel")
    session.commit()
    checks = {c.anchor_id: c for c in locate(session, anchors)}
    assert checks["A2"].archived is False
    assert "не подписан" in (checks["A2"].note or "")


# ── calibration ─────────────────────────────────────────────────────────────

def _config() -> ScorerConfig:
    return ScorerConfig(rubric_dir=default_rubric_dir(), model="fake-model", max_chars=12000)


async def test_a_model_that_agrees_with_the_anchors_is_within_tolerance(session, anchors):
    _seed_channel(session)
    llm = FakeScorerLLM(_answers_matching(anchors))
    checks = await calibrate(
        anchors,
        session_factory=lambda: _session(session),
        llm=llm,
        modules=load_modules(),
    )
    scored = [c for c in checks if c.model]
    assert len(scored) == 5
    assert all(c.verdict == "ok" for c in scored)
    assert scored[0].model_kpd == pytest.approx(scored[0].reference_kpd)
    # Every axis matches except the one the rubric clamps: A2 is a "трактат" with I=9.5,
    # while rule 12 caps I for a трактат at 9.0. Calibration reports that, by design.
    by_id = {c.anchor_id: c for c in scored}
    assert by_id["A3"].deltas == {axis: 0.0 for axis in ("I", "V", "U", "P", "S", "D")}
    assert by_id["A2"].deltas["I"] == pytest.approx(-0.5)
    assert "genre ceiling" in (by_id["A2"].note or "")


async def test_a_systematic_shift_shows_up_per_axis(session, anchors):
    _seed_channel(session)
    # S, not U: an axis already at 10 cannot move up, it just clips
    llm = FakeScorerLLM(_answers_matching(anchors, shift={"S": -1.0}))
    checks = await calibrate(
        anchors,
        session_factory=lambda: _session(session),
        llm=llm,
        modules=load_modules(),
    )
    scored = [c for c in checks if c.model]
    assert all(c.verdict == "drift" for c in scored)
    assert all(c.deltas["S"] == -1.0 for c in scored)
    summary = axis_summary(checks)
    assert summary["S"] == {"mean": -1.0, "mean_abs": 1.0, "n": 5}
    # I is shifted only by the трактат cap on A2: one anchor of five, minus half a point
    assert summary["I"] == {"mean": -0.1, "mean_abs": 0.1, "n": 5}


async def test_one_axis_off_by_more_than_tolerance_is_drift(session, anchors):
    _seed_channel(session)
    llm = FakeScorerLLM(_answers_matching(anchors, shift={"V": -(TOLERANCE + 0.1)}))
    checks = await calibrate(
        anchors,
        session_factory=lambda: _session(session),
        llm=llm,
        modules=load_modules(),
    )
    scored = [c for c in checks if c.model]
    assert all(c.verdict == "drift" for c in scored)
    assert scored[0].worst_delta == pytest.approx(TOLERANCE + 0.1)
    assert scored[0].deltas["V"] == pytest.approx(-(TOLERANCE + 0.1))


async def test_the_anchor_prompt_has_no_channel_trajectory(session, anchors):
    """An anchor is a global reference: it is judged without a channel's trend."""
    _seed_channel(session)
    llm = FakeScorerLLM(_answers_matching(anchors))
    await calibrate(
        anchors,
        session_factory=lambda: _session(session),
        llm=llm,
        modules=load_modules(),
    )
    first_user = llm.prompts[0][1]
    assert "канал оценивается впервые" in first_user
    assert "TREND:" not in first_user


async def test_a_bad_key_aborts_the_calibration(session, anchors):
    """One auth failure is configuration: do not ask five times."""
    _seed_channel(session)
    llm = FakeScorerLLM([LLMError("HTTP 401 — authentication failed", retryable=False)] * 5)
    with pytest.raises(LLMError):
        await calibrate(
            anchors,
            session_factory=lambda: _session(session),
            llm=llm,
            modules=load_modules(),
        )
    assert len(llm.prompts) == 1


async def test_a_failing_backend_marks_only_that_anchor(session, anchors):
    _seed_channel(session)
    llm = FakeScorerLLM([LLMError("down")])
    checks = await calibrate(
        anchors,
        session_factory=lambda: _session(session),
        llm=llm,
        modules=load_modules(),
    )
    by_id = {c.anchor_id: c for c in checks}
    assert by_id["A1"].verdict == "unscored"
    assert "down" in (by_id["A1"].error or "")
    # the rest are still attempted
    assert by_id["A2"].model != {}


async def test_calibration_never_writes_scores(session, anchors):
    _seed_channel(session)
    llm = FakeScorerLLM(_answers_matching(anchors))
    await calibrate(
        anchors,
        session_factory=lambda: _session(session),
        llm=llm,
        modules=load_modules(),
    )
    assert session.exec(select(PostScore)).all() == []
    assert session.exec(select(ChannelState)).all() == []


async def test_only_a_subset_can_be_calibrated(session, anchors):
    _seed_channel(session)
    llm = FakeScorerLLM(_answers_matching(anchors))
    checks = await calibrate(
        anchors,
        session_factory=lambda: _session(session),
        llm=llm,
        modules=load_modules(),
        only=["A3"],
    )
    scored = [c for c in checks if c.model]
    assert [c.anchor_id for c in scored] == ["A3"]
    assert len(llm.prompts) == 1


def _session(session):
    """Reuse the test session but through a context manager, as the loader expects."""

    class _Ctx:
        def __enter__(self):
            return session

        def __exit__(self, *exc_info):
            session.flush()
            return False

    return _Ctx()
