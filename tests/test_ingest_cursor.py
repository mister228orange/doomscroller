"""The invariant that matters most: a failure must never lose messages.

The previous implementation persisted the cursor *before* processing, so any failure
silently dropped the batch forever. These tests pin the corrected behaviour.
"""

import pytest
from sqlmodel import select

from doomscroller import ingest
from doomscroller.config import Settings
from doomscroller.models import Message, MessageEvent
from doomscroller.store import record_event
from doomscroller.subscriptions import subscribe

from fakes import FakeClient, FakeMessage


def _settings(batch_size=100) -> Settings:
    return Settings(api_id=1, api_hash="x", batch_size=batch_size)


async def test_poll_stores_messages_and_advances_cursor(session):
    sub = subscribe(session, "chan")
    session.commit()
    client = FakeClient([FakeMessage(1, "a"), FakeMessage(2, "b")])

    count = await ingest.poll_subscription(client, session, sub, _settings())

    assert count == 2
    assert sub.cursor_message_id == 2
    assert sub.chat_id is not None
    assert len(session.exec(select(Message)).all()) == 2


async def test_cursor_does_not_advance_when_storage_fails(session, monkeypatch):
    sub = subscribe(session, "chan")
    session.commit()
    client = FakeClient([FakeMessage(1, "a"), FakeMessage(2, "b")])

    real = ingest.record_event
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("storage exploded")
        return real(*args, **kwargs)

    monkeypatch.setattr(ingest, "record_event", flaky)

    with pytest.raises(RuntimeError):
        await ingest.poll_subscription(client, session, sub, _settings())

    session.rollback()
    refreshed = session.exec(select(type(sub)).where(type(sub).chat_ref == "chan")).one()
    assert refreshed.cursor_message_id == 0, "cursor advanced despite a failed batch"
    assert session.exec(select(Message)).all() == [], "partial batch was committed"


async def test_repolling_is_idempotent(session):
    sub = subscribe(session, "chan")
    session.commit()
    client = FakeClient([FakeMessage(1, "a"), FakeMessage(2, "b")])

    assert await ingest.poll_subscription(client, session, sub, _settings()) == 2
    assert await ingest.poll_subscription(client, session, sub, _settings()) == 0

    assert len(session.exec(select(Message)).all()) == 2
    assert len(session.exec(select(MessageEvent)).all()) == 2


async def test_poll_pages_through_a_backlog(session):
    """A batch_size smaller than the backlog must not truncate it."""
    sub = subscribe(session, "chan")
    session.commit()
    client = FakeClient([FakeMessage(i, f"m{i}") for i in range(1, 8)])

    count = await ingest.poll_subscription(client, session, sub, _settings(batch_size=2))

    assert count == 7
    assert sub.cursor_message_id == 7
    assert len(session.exec(select(Message)).all()) == 7


async def test_poll_continues_from_stored_cursor(session):
    sub = subscribe(session, "chan")
    sub.chat_id = -1001234567890
    sub.cursor_message_id = 3
    session.commit()
    client = FakeClient([FakeMessage(i, f"m{i}") for i in range(1, 6)])
    client.entity.id = -1001234567890

    count = await ingest.poll_subscription(client, session, sub, _settings())

    assert count == 2, "should only pick up 4 and 5"
    assert [m.message_id for m in session.exec(select(Message)).all()] == [4, 5]


async def test_disabled_subscription_is_skipped(session):
    sub = subscribe(session, "chan")
    sub.enabled = False
    session.commit()
    client = FakeClient([FakeMessage(1, "a")])

    assert await ingest.poll_subscription(client, session, sub, _settings()) == 0
    assert session.exec(select(Message)).all() == []
