"""Telethon stand-ins so ingest can be tested without Telegram."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


class FakeEntity:
    def __init__(self, chat_id: int = -1001234567890, title: str = "Fake Channel"):
        self.id = chat_id
        self.title = title
        self.broadcast = True

    @property
    def chat_id(self) -> int:
        return self.id


class FakeMessage:
    def __init__(self, message_id: int, text: str, sender_id: Optional[int] = 42):
        self.id = message_id
        self.text = text
        self.date = datetime.now(timezone.utc)
        self.edit_date = None
        self.sender_id = sender_id
        self.sender = None
        self.reply_to_msg_id = None
        self.views = None
        self.forwards = None


class FakeClient:
    """Minimal async Telethon client: get_entity + get_messages(min_id=...)."""

    def __init__(self, messages: list[FakeMessage], entity: Any | None = None):
        self._messages = sorted(messages, key=lambda m: m.id)
        self.entity = entity or FakeEntity()

    async def get_entity(self, ref):  # noqa: ANN001
        return self.entity

    async def get_messages(self, entity, limit: int = 100, min_id: int = 0, reverse: bool = False, **kw):  # noqa: ANN001
        rows = [m for m in self._messages if m.id > (min_id or 0)]
        return rows[:limit]
