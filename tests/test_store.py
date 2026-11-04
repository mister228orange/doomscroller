"""Store invariants: idempotent ingest and append-only events."""

from sqlmodel import select

from doomscroller.models import Message, MessageEvent
from doomscroller.store import events_for_message, record_event, stats


def _new(session, mid=1, text="hello", **kw):
    return record_event(session, chat_id=100, message_id=mid, event_type="new", text=text, **kw)


def test_new_message_creates_row_and_event(session):
    result = _new(session)
    session.commit()
    assert result.created is True
    assert result.event_appended is True
    assert session.exec(select(Message)).one().text == "hello"
    assert len(session.exec(select(MessageEvent)).all()) == 1


def test_recording_the_same_new_message_twice_is_idempotent(session):
    """Re-polling re-delivers messages; that must not duplicate anything."""
    _new(session, mid=7, text="once")
    session.commit()
    again = _new(session, mid=7, text="once")
    session.commit()

    assert again.created is False
    assert again.event_appended is False
    assert len(session.exec(select(Message)).all()) == 1
    assert len(session.exec(select(MessageEvent)).all()) == 1, "duplicate event appended"


def test_edit_updates_row_and_appends_event(session):
    _new(session, mid=5, text="before")
    session.commit()
    record_event(session, chat_id=100, message_id=5, event_type="edited", text="after")
    session.commit()

    message = session.exec(select(Message)).one()
    assert message.text == "after"
    assert message.edited_at is not None
    kinds = [e.event_type for e in events_for_message(session, 100, 5)]
    assert kinds == ["new", "edited"], "event history must be append-only"


def test_delete_marks_row_but_preserves_archived_text(session):
    """A delete event must not wipe what we already archived."""
    _new(session, mid=9, text="important")
    session.commit()
    record_event(session, chat_id=100, message_id=9, event_type="deleted")
    session.commit()

    message = session.exec(select(Message)).one()
    assert message.deleted is True
    assert message.text == "important"
    assert [e.event_type for e in events_for_message(session, 100, 9)] == ["new", "deleted"]


def test_identical_repeated_edits_each_recorded(session):
    """Edits are genuinely distinct events — all of them are kept."""
    _new(session, mid=3, text="v1")
    session.commit()
    for text in ("v2", "v3", "v4"):
        record_event(session, chat_id=100, message_id=3, event_type="edited", text=text)
    session.commit()
    assert [e.event_type for e in events_for_message(session, 100, 3)] == [
        "new", "edited", "edited", "edited",
    ]
    assert session.exec(select(Message)).one().text == "v4"


def test_invalid_event_type_rejected(session):
    import pytest

    with pytest.raises(ValueError):
        record_event(session, chat_id=1, message_id=1, event_type="nonsense")


def test_stats_counts(session):
    _new(session, mid=1)
    _new(session, mid=2)
    record_event(session, chat_id=100, message_id=2, event_type="deleted")
    session.commit()
    s = stats(session)
    assert s["messages"] == 2
    assert s["message_events"] == 3
    assert s["deleted_messages"] == 1
