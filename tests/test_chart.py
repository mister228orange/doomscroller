"""The channel rating page: data collection, self-contained HTML, ASCII view."""

from __future__ import annotations

import pytest
from sqlmodel import select

from doomscroller.chart import collect_chart_data, render_ascii, render_html
from doomscroller.models import ChannelState, PostScore
from doomscroller.store import record_event
from doomscroller.subscriptions import subscribe

CHAT = -1007770003333


@pytest.fixture
def scored_channel(session):
    """Twelve scored posts with a visible KPD arc, plus the channel state row."""
    subscribe(session, "charttest")
    sub = session.exec(select(__import__("doomscroller.models", fromlist=["Subscription"]).Subscription)).one()
    sub.chat_id = CHAT
    kpds = [3.0, 4.5, 6.0, 7.5, 8.5, 9.0, 8.0, 7.0, 6.5, 5.5, 4.0, 2.0]
    spheres = ["TECH", "SCI", "POWER", "ECON", "SOC", "CULT", "TECH", "SCI", "POWER", "ECON", "SOC", "CULT"]
    for index, (kpd, sphere) in enumerate(zip(kpds, spheres), start=1):
        record_event(
            session,
            chat_id=CHAT,
            message_id=index,
            event_type="new",
            text=f"Пост номер {index} с достаточным количеством текста для заголовка",
            sender_name="author",
        )
        session.add(
            PostScore(
                chat_id=CHAT,
                message_id=index,
                status="scored",
                primary=sphere,
                secondary=["HIER"] if index % 2 else ["DEGR"],
                title=f"Пост номер {index}",
                score_i=kpd,
                score_v=kpd - 1,
                score_u=kpd,
                score_p=5.0,
                score_s=6.0,
                score_d=kpd + 1,
                kpd=kpd,
                eff=1.5,
                model="test-model",
                prompt_version="test0000",
            )
        )
    session.add(
        ChannelState(
            chat_id=CHAT,
            chat_ref="charttest",
            title="Test Channel",
            profile_description="Профиль канала для теста",
            summary="Сводка канала для теста",
            total_scored=12,
            total_skipped=1,
            total_failed=0,
            kpd_mean=sum(kpds) / len(kpds),
            kpd_max=max(kpds),
        )
    )
    session.commit()
    return session


def test_collect_orders_posts_and_counts_tags(scored_channel):
    data = collect_chart_data(scored_channel, CHAT)
    assert [post["message_id"] for post in data["posts"]] == list(range(1, 13))
    assert data["counters"] == {"scored": 12, "skipped": 1, "failed": 0}
    assert data["primary_counts"]["TECH"] == 2
    assert data["secondary_counts"]["HIER"] == 6
    assert data["models"] == ["test-model"]


def test_html_is_self_contained(scored_channel):
    """No external assets: everything inline, links only point back at Telegram."""
    html = render_html(collect_chart_data(scored_channel, CHAT))
    assert "<svg" in html and "</svg>" in html
    assert "Рейтинг канала charttest" in html
    assert "Профиль канала для теста" in html
    assert "Пост номер 6" in html  # the KPD peak is in the top table
    assert "src=" not in html  # no scripts, no images fetched from anywhere
    assert "t.me/charttest/6" in html


def test_ascii_view_sparkles_the_arc(scored_channel, capsys):
    text = render_ascii(collect_chart_data(scored_channel, CHAT))
    assert "charttest" in text
    assert "▁" in text and ("▇" in text or "█" in text)  # the rise and the fall
    assert "топ:" in text


def test_cmd_chart_writes_the_file(scored_channel, tmp_path, monkeypatch):
    from doomscroller.cli import main

    out = tmp_path / "rating.html"
    code = main(["chart", "charttest", "--out", str(out)])
    assert code == 0
    assert out.exists() and out.read_text(encoding="utf-8").startswith("<!doctype html>")
