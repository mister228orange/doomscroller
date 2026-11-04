"""Getting messages back out: any amount, paged, or just what's new."""

from doomscroller.store import (
    fetch_messages,
    iter_all_messages,
    new_messages,
    record_event,
    search_messages,
)
from doomscroller.subscriptions import subscribe


def _seed(session, chat_id=100, count=25):
    for i in range(1, count + 1):
        record_event(
            session,
            chat_id=chat_id,
            message_id=i,
            event_type="new",
            text=f"message {i}" + (" grant" if i % 5 == 0 else ""),
            sender_id=7,
            sender_name="alice" if i % 2 else "bob",
        )
    session.commit()


def test_fetch_respects_limit_newest_first(session):
    _seed(session)
    rows = fetch_messages(session, 100, limit=5)
    assert [m.message_id for m in rows] == [25, 24, 23, 22, 21]


def test_fetch_oldest_first(session):
    _seed(session)
    rows = fetch_messages(session, 100, limit=3, newest_first=False)
    assert [m.message_id for m in rows] == [1, 2, 3]


def test_pagination_with_before_and_after(session):
    _seed(session)
    older = fetch_messages(session, 100, limit=5, before_id=11)
    assert [m.message_id for m in older] == [10, 9, 8, 7, 6]
    newer = fetch_messages(session, 100, limit=5, after_id=20, newest_first=False)
    assert [m.message_id for m in newer] == [21, 22, 23, 24, 25]


def test_iter_all_streams_everything_in_batches(session):
    """'Any amount' must not mean 'whatever fits in one query'."""
    _seed(session, count=250)
    got = list(iter_all_messages(session, 100, batch_size=10))
    assert len(got) == 250
    assert [m.message_id for m in got] == list(range(1, 251))


def test_iter_all_can_resume_from_a_cursor(session):
    _seed(session, count=50)
    got = list(iter_all_messages(session, 100, after_id=45))
    assert [m.message_id for m in got] == [46, 47, 48, 49, 50]


def test_new_messages_uses_subscription_cursor(session):
    _seed(session, count=10)
    sub = subscribe(session, "chan")
    sub.chat_id = 100
    sub.cursor_message_id = 8
    session.commit()

    rows = new_messages(session, 100)
    assert [m.message_id for m in rows] == [9, 10]


def test_new_messages_honours_explicit_since(session):
    _seed(session, count=10)
    rows = new_messages(session, 100, since_id=3)
    assert [m.message_id for m in rows] == [4, 5, 6, 7, 8, 9, 10]


def test_deleted_messages_excluded_by_default(session):
    _seed(session, count=3)
    record_event(session, chat_id=100, message_id=2, event_type="deleted")
    session.commit()

    assert [m.message_id for m in fetch_messages(session, 100, limit=10, newest_first=False)] == [1, 3]
    with_deleted = fetch_messages(session, 100, limit=10, newest_first=False, include_deleted=True)
    assert [m.message_id for m in with_deleted] == [1, 2, 3]


def test_search_across_chats_and_scoped_to_one(session):
    _seed(session, chat_id=100, count=10)
    _seed(session, chat_id=200, count=10)
    hits = search_messages(session, "grant", limit=100)
    assert {h.chat_id for h in hits} == {100, 200}
    scoped = search_messages(session, "grant", chat_id=100, limit=100)
    assert {h.chat_id for h in scoped} == {100}


def test_search_matches_sender_name(session):
    _seed(session, count=4)
    assert search_messages(session, "alice", limit=10)
