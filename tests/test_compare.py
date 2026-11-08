"""The cross-channel comparison table."""

from __future__ import annotations

import pytest
from sqlmodel import select

from doomscroller.compare import collect_comparison, render_table
from doomscroller.models import ChannelState, PostScore
from doomscroller.store import record_event
from doomscroller.subscriptions import subscribe

A = -1007770004444
B = -1007770005555


def _seed_channel(session, chat_id: int, ref: str, kpds: list[float], primary: str) -> None:
    subscribe(session, ref)
    from doomscroller.models import Subscription

    sub = session.exec(select(Subscription).where(Subscription.chat_ref == ref)).one()
    sub.chat_id = chat_id
    for index, kpd in enumerate(kpds, start=1):
        record_event(
            session,
            chat_id=chat_id,
            message_id=index,
            event_type="new",
            text=f"Пост {index} канала {ref} с достаточной длиной для заголовка поста",
            sender_name="author",
        )
        session.add(
            PostScore(
                chat_id=chat_id,
                message_id=index,
                status="scored",
                primary=primary,
                title=f"Пост {index} канала {ref}",
                score_i=kpd,
                score_v=kpd,
                score_u=kpd,
                score_p=5.0,
                score_s=5.0,
                score_d=kpd,
                kpd=kpd,
                eff=1.5,
                model="test-model",
                prompt_version="test0000",
            )
        )
    session.add(
        ChannelState(
            chat_id=chat_id,
            chat_ref=ref,
            title=ref.upper(),
            total_scored=len(kpds),
            total_skipped=2,
            total_failed=0,
            kpd_mean=sum(kpds) / len(kpds),
            kpd_max=max(kpds),
            drift_note=f"дрейф {ref}",
        )
    )
    session.commit()


@pytest.fixture
def two_channels(session):
    _seed_channel(session, A, "alpha", [8.0, 9.0, 7.0], "TECH")
    _seed_channel(session, B, "beta", [4.0, 5.0, 6.0], "CULT")
    return session


def test_collect_computes_spread_and_spheres(two_channels):
    data = collect_comparison(two_channels, ["alpha", "beta"])
    assert [item["chat"] for item in data] == ["alpha", "beta"]
    alpha, beta = data
    assert alpha["scored"] == 3 and alpha["skipped"] == 2
    assert alpha["kpd_mean"] == 8.0 and alpha["kpd_min"] == 7.0 and alpha["kpd_max"] == 9.0
    assert alpha["kpd_median"] == 8.0
    assert alpha["axes"]["I"] == 8.0
    assert alpha["top_spheres"] == ["TECH 3"]
    assert alpha["best"]["message_id"] == 2 and alpha["best"]["kpd"] == 9.0
    assert beta["kpd_mean"] == 5.0


def test_missing_channel_is_reported_not_fatal(two_channels):
    data = collect_comparison(two_channels, ["alpha", "ghost"])
    assert data[1] == {"chat": "ghost", "missing": True}


def test_table_renders_channels_as_columns(two_channels, capsys):
    text = render_table(collect_comparison(two_channels, ["alpha", "beta"]))
    assert "метрика" in text and "alpha" in text and "beta" in text
    assert "КПД средн" in text
    assert "лучший alpha: КПД 9.00 #2" in text
    assert "дрейф beta: дрейф beta" in text


def test_no_module_imports_sqlalchemy_select():
    """SQLModel sessions need sqlmodel's select; sqlalchemy's returns bare rows.

    This bit three modules in one evening (chart, ocr, compare) — so it is now a rule
    with a guard, not a habit.
    """
    import re
    from pathlib import Path

    package = Path(__import__("doomscroller").__file__).parent
    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        if path.name == "models.py":
            continue  # column types only
        text = path.read_text(encoding="utf-8")
        for block in re.findall(r"from sqlalchemy import \(([^)]*)\)|from sqlalchemy import ([^\n(]+)", text):
            names = "".join(block)
            if re.search(r"\bselect\b", names):
                offenders.append(path.name)
    assert offenders == [], f"import select from sqlmodel, not sqlalchemy: {offenders}"
