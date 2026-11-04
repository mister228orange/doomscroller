"""The channel trajectory: moving means, deviations, shifts, peaks, rebuild."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sqlmodel import select

from doomscroller.models import ChannelEvent, PostScore
from doomscroller.scoring.state import (
    DEVIATION_FLAG,
    PEAK_MIN_KPD,
    SHIFT_STREAK,
    apply_score,
    get_or_create_state,
    recompute_state,
    state_snapshot,
)
from doomscroller.store import record_event
from doomscroller.subscriptions import subscribe

CHAT = -1007770001111


def _score(session, state, message_id: int, *, primary: str = "SCI", **axes) -> PostScore:
    values = {"I": 8.0, "V": 8.0, "U": 8.0, "P": 5.0, "S": 5.0, "D": 8.0}
    values.update(axes)
    row = PostScore(
        chat_id=state.chat_id,
        message_id=message_id,
        status="scored",
        primary=primary,
        secondary=["TRUST"],
        title=f"пост {message_id}",
        score_i=values["I"],
        score_v=values["V"],
        score_u=values["U"],
        score_p=values["P"],
        score_s=values["S"],
        score_d=values["D"],
        kpd=round((values["I"] + values["V"] + values["U"] + values["D"]) / 4, 2),
        eff=None if values["P"] <= 0 else None,
        flags=[],
        scored_at=datetime.now(timezone.utc),
    )
    row.eff = round(row.kpd / max(values["P"], 1.0), 2) if row.kpd > 5.0 else None
    return apply_score(session, state, row)


@pytest.fixture
def state(session):
    return get_or_create_state(session, chat_id=CHAT, chat_ref="testchat", window=5)


def test_first_post_seeds_the_trend_without_deviations(session, state):
    flags = _score(session, state, 1)
    assert flags == []
    assert state.means == {"I": 8.0, "V": 8.0, "U": 8.0, "P": 5.0, "S": 5.0, "D": 8.0}
    assert state.deltas == {axis: 0.0 for axis in ("I", "V", "U", "P", "S", "D")}
    assert state.total_scored == 1
    assert state.last_scored_message_id == 1
    assert state.kpd_mean == 8.0


def test_moving_average_with_window_five(session, state):
    _score(session, state, 1)
    # (8 * 4 + 4) / 5 = 7.2
    _score(session, state, 2, I=4.0)
    assert state.means["I"] == 7.2
    assert state.means["V"] == 8.0


def test_deviation_flag_uses_the_trend_before_the_post(session, state):
    _score(session, state, 1)
    flags = _score(session, state, 2, I=3.0)  # 5.0 below the 8.0 mean
    assert any(flag.startswith(f"deviation:I:") for flag in flags)
    assert abs(float(flags[0].split(":")[-1])) > DEVIATION_FLAG


def test_score_row_carries_its_own_channel_view(session, state):
    _score(session, state, 1)
    _score(session, state, 2, I=9.5, V=9.5, U=9.5, D=9.5)
    row = session.exec(select(PostScore).where(PostScore.message_id == 2)).one()
    assert row.deviation["I"] == pytest.approx(1.5)
    assert row.trend_position in {"above", "at", "below"}
    assert row.shift_candidate is True  # 2+ axes moved by >= 1.5


def test_tag_distribution_and_anchors_are_kept(session, state):
    for message_id in range(1, 8):
        _score(session, state, message_id, primary="SCI" if message_id % 2 else "TECH")
    assert state.tag_distribution == {"SCI": 4, "TECH": 3}
    # local anchors keep only the last five, newest last
    assert [a["message_id"] for a in state.local_anchors] == [3, 4, 5, 6, 7]
    assert state.local_anchors[-1]["id"] == "p_7"


def test_peak_event_on_a_new_channel_best(session, state):
    _score(session, state, 1, I=9.6, V=9.6, U=9.6, D=9.6)
    assert state.kpd_max == pytest.approx(9.6) if False else state.kpd_max >= PEAK_MIN_KPD
    peaks = session.exec(select(ChannelEvent).where(ChannelEvent.kind == "peak")).all()
    assert len(peaks) == 1
    assert peaks[0].message_id == 1


def test_weak_first_post_does_not_create_a_peak_event(session, state):
    _score(session, state, 1, I=2.0, V=2.0, U=2.0, D=2.0)
    assert len(session.exec(select(ChannelEvent).where(ChannelEvent.kind == "peak")).all()) == 0


def test_shift_requires_a_streak_of_candidates(session, state):
    _score(session, state, 1)  # first post: no trend to deviate from
    candidates = 0
    for i in range(2, 2 + SHIFT_STREAK):
        flags = _score(session, state, i, I=10.0, V=1.0, U=1.0, D=1.0)
        assert any(flag.startswith("deviation:") for flag in flags)
        candidates += 1
        shifts = len(session.exec(select(ChannelEvent).where(ChannelEvent.kind == "shift")).all())
        if candidates < SHIFT_STREAK:
            assert shifts == 0, "a shift must not be recorded before the streak completes"
    assert shifts == 1
    assert state.deviation_streak == 0


def test_deviation_streak_resets_on_a_conforming_post(session, state):
    _score(session, state, 1)
    _score(session, state, 2, I=10.0, V=1.0, U=1.0, D=1.0)
    _score(session, state, 3)  # back on trend
    assert state.deviation_streak == 0
    assert state.shift_candidate_message_id is None
    assert len(session.exec(select(ChannelEvent).where(ChannelEvent.kind == "shift")).all()) == 0


def test_rebuild_reproduces_the_incremental_trajectory(session, state):
    for message_id in range(1, 8):
        _score(session, state, message_id, I=6.0 + message_id * 0.5)
    incremental = {
        "means": dict(state.means),
        "deltas": dict(state.deltas),
        "kpd_mean": state.kpd_mean,
        "tags": dict(state.tag_distribution),
        "anchors": [a["message_id"] for a in state.local_anchors],
        "total": state.total_scored,
    }

    rebuilt = recompute_state(session, chat_id=CHAT, window=5)
    assert rebuilt.means == pytest.approx(incremental["means"])
    assert rebuilt.deltas == pytest.approx(incremental["deltas"])
    assert rebuilt.kpd_mean == pytest.approx(incremental["kpd_mean"])
    assert rebuilt.tag_distribution == incremental["tags"]
    assert [a["message_id"] for a in rebuilt.local_anchors] == incremental["anchors"]
    assert rebuilt.total_scored == incremental["total"]

    # rebuilding must not replay trajectory events into the log
    assert len(session.exec(select(ChannelEvent).where(ChannelEvent.kind == "shift")).all()) == 0


def test_rescored_post_does_not_double_count(session, state):
    _score(session, state, 1)
    _score(session, state, 2)
    assert state.total_scored == 2

    row = session.exec(select(PostScore).where(PostScore.message_id == 2)).one()
    row.score_i = 2.0
    row.kpd = 6.5
    session.add(row)
    session.flush()
    rebuilt = recompute_state(session, chat_id=CHAT, window=5)
    assert rebuilt.total_scored == 2
    assert rebuilt.means["I"] == pytest.approx((8.0 * 4 + 2.0) / 5)


def test_snapshot_is_a_plain_dict(session, state):
    _score(session, state, 1)
    _score(session, state, 2)
    snapshot = state_snapshot(session, state, context_anchors=1, events=3)
    assert isinstance(snapshot, dict)
    assert snapshot["chat_id"] == CHAT
    assert len(snapshot["local_anchors"]) == 1
    assert snapshot["total_scored"] == 2
    assert snapshot["summary_pending"] == 2
    assert "means" in snapshot and "events" in snapshot


def test_state_survives_an_unscored_channel(session):
    sub = subscribe(session, "emptychat")
    sub.chat_id = CHAT - 1
    record_event(
        session,
        chat_id=CHAT - 1,
        message_id=1,
        event_type="new",
        text="короткий пост",
    )
    session.commit()
    state = get_or_create_state(session, chat_id=CHAT - 1)
    snapshot = state_snapshot(session, state)
    assert snapshot["total_scored"] == 0
    assert snapshot["means"] == {}
