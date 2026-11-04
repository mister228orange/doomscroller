"""Managing the subscription list."""

from __future__ import annotations

from typing import Optional

from sqlmodel import Session, select

from .models import Subscription


def normalise_ref(ref: str) -> str:
    """Accept @name, name, t.me/name, https://t.me/name, or a numeric id."""
    ref = (ref or "").strip()
    if not ref:
        raise ValueError("empty chat reference")
    for prefix in ("https://", "http://"):
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
    if ref.startswith("t.me/"):
        ref = ref[len("t.me/"):]
    ref = ref.strip().strip("/")
    # Telegram treats @name and name as the same chat; canonicalise so that
    # "@chan", "chan" and "t.me/chan" cannot become three subscriptions.
    if ref.startswith("@"):
        ref = ref[1:]
    return ref


def subscribe(session: Session, ref: str, *, title: str | None = None) -> Subscription:
    """Add a subscription. Idempotent: subscribing twice returns the existing row."""
    norm = normalise_ref(ref)
    existing = session.exec(select(Subscription).where(Subscription.chat_ref == norm)).first()
    if existing is not None:
        if title and not existing.title:
            existing.title = title
        return existing
    sub = Subscription(chat_ref=norm, title=title)
    session.add(sub)
    session.flush()
    return sub


def unsubscribe(session: Session, ref: str) -> bool:
    norm = normalise_ref(ref)
    sub = session.exec(select(Subscription).where(Subscription.chat_ref == norm)).first()
    if sub is None:
        return False
    session.delete(sub)
    return True


def set_enabled(session: Session, ref: str, enabled: bool) -> Optional[Subscription]:
    norm = normalise_ref(ref)
    sub = session.exec(select(Subscription).where(Subscription.chat_ref == norm)).first()
    if sub is None:
        return None
    sub.enabled = enabled
    return sub


def list_subscriptions(session: Session, *, enabled_only: bool = False) -> list[Subscription]:
    stmt = select(Subscription).order_by(Subscription.chat_ref.asc())
    if enabled_only:
        stmt = stmt.where(Subscription.enabled == True)  # noqa: E712
    return list(session.exec(stmt).all())


def get_subscription(session: Session, ref: str) -> Optional[Subscription]:
    norm = normalise_ref(ref)
    return session.exec(select(Subscription).where(Subscription.chat_ref == norm)).first()


def sync_from_env(session: Session, refs: list[str]) -> list[Subscription]:
    """Ensure everything listed in SUBSCRIPTIONS exists (does not remove others)."""
    return [subscribe(session, r) for r in refs]
