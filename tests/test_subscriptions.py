"""Subscription bookkeeping."""

import pytest

from doomscroller.subscriptions import (
    get_subscription,
    list_subscriptions,
    normalise_ref,
    set_enabled,
    subscribe,
    sync_from_env,
    unsubscribe,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("@channel", "channel"),
        ("channel", "channel"),
        ("t.me/channel", "channel"),
        ("https://t.me/channel", "channel"),
        ("https://t.me/channel/", "channel"),
        ("-1001234567890", "-1001234567890"),
    ],
)
def test_normalise_ref(raw, expected):
    assert normalise_ref(raw) == expected


def test_normalise_rejects_empty():
    with pytest.raises(ValueError):
        normalise_ref("   ")


def test_subscribe_is_idempotent(session):
    a = subscribe(session, "@chan")
    session.commit()
    b = subscribe(session, "t.me/chan")  # same chat, different spelling
    session.commit()
    assert a.chat_ref == b.chat_ref
    assert len(list_subscriptions(session)) == 1


def test_unsubscribe(session):
    subscribe(session, "chan")
    session.commit()
    assert unsubscribe(session, "@chan") is True
    assert unsubscribe(session, "@chan") is False
    assert list_subscriptions(session) == []


def test_enable_disable(session):
    subscribe(session, "chan")
    session.commit()
    assert set_enabled(session, "chan", False).enabled is False
    assert set_enabled(session, "chan", True).enabled is True
    assert set_enabled(session, "nope", True) is None


def test_sync_from_env_adds_missing_without_removing(session):
    subscribe(session, "existing")
    session.commit()
    sync_from_env(session, ["existing", "new-one"])
    session.commit()
    refs = {s.chat_ref for s in list_subscriptions(session)}
    assert refs == {"existing", "new-one"}


def test_get_subscription(session):
    subscribe(session, "@chan")
    session.commit()
    assert get_subscription(session, "t.me/chan") is not None
    assert get_subscription(session, "missing") is None
