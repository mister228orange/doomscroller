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


# ── scoring fakes ─────────────────────────────────────────────────────────────


def score_answer(
    *,
    i: float = 8.0,
    v: float = 7.5,
    u: float = 7.0,
    p: float = 5.0,
    s: float = 6.0,
    d: float = 8.0,
    primary: str = "SCI",
    secondary: list[str] | None = None,
    genre: str = "пост",
    thesis: str | None = None,
    flags: list[str] | None = None,
    confidence: str = "medium",
    nearest_anchors: list[str] | None = None,
    extra: dict | None = None,
) -> str:
    """A well-formed answer in the shape ``06_procedure.md`` asks for.

    Deliberately includes prose and a markdown table around the JSON: real answers do, and
    the parser must survive it.
    """
    import json

    payload = {
        "genre": genre,
        "primary": primary,
        "secondary": secondary if secondary is not None else ["TRUST"],
        "main_thesis": thesis or "Тезис по умолчанию: инструмент, а не информация.",
        "clauses": {"total": 5, "fact": 1, "arg": 3, "slogan": 1},
        "density_type": "concentration",
        "flags": flags or [],
        "scores": {"I": i, "V": v, "U": u, "P": p, "S": s, "D": d},
        "justification": {
            "I": "держит внимание",
            "V": "даёт механизм",
            "U": "свой ход",
            "P": "нужен общий кругозор",
            "S": "усмешка с горечью",
            "D": "пять тезисов в пяти фразах",
        },
        "KPD": 0.0,
        "Eff": 0.0,
        "nearest_anchors": nearest_anchors if nearest_anchors is not None else ["A3", "A4"],
        "confidence": confidence,
    }
    if extra:
        payload.update(extra)
    return (
        "## Разбор\n"
        "Тезис: тестовая проверка формата. Клаузы: 5. Тип плотности: concentration.\n\n"
        "## JSON\n"
        "```json\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```\n\n"
        "## Таблица\n"
        "| A | SCI+TRUST | 8.0 | 7.5 | 7.0 | 5.0 | 6.0 | 8.0 | 7.62 | 1.52 |\n"
    )


def summary_answer(profile: str = "Канал о методе и языке.", summary: str = "темы: метод; мало данных") -> str:
    import json

    return json.dumps(
        {
            "profile": profile,
            "summary": summary,
            "primary_dominant": "SCI",
            "secondary_dominant": ["TRUST"],
            "drift_note": None,
        },
        ensure_ascii=False,
    )


class FakeScorerLLM:
    """A ``ChatBackend`` with scripted answers.

    ``answers`` entries are either strings (returned in order) or exceptions (raised),
    which is how the tests exercise failure paths without a network.
    """

    def __init__(self, answers: list, default: str | None = None, summary_default: str | None = None):
        self.answers = list(answers)
        self.default = default if default is not None else score_answer()
        self.summary_default = summary_default if summary_default is not None else summary_answer()
        self.prompts: list[tuple[str, str]] = []

    async def chat(self, *, system: str, user: str):
        from doomscroller.scoring.llm import LLMResponse

        self.prompts.append((system, user))
        if self.answers:
            answer = self.answers.pop(0)
        elif "ОБНОВИТЬ ПАМЯТЬ КАНАЛА" in system:
            answer = self.summary_default
        else:
            answer = self.default
        if isinstance(answer, Exception):
            raise answer
        return LLMResponse(
            text=answer, model="fake-model", prompt_tokens=1000, completion_tokens=100
        )

    async def chat_raw(self, messages, *, json_mode=None, max_tokens=None):
        """Raw-message variant used by the OCR engine (vision payloads)."""
        from doomscroller.scoring.llm import LLMResponse

        self.raw_prompts = getattr(self, "raw_prompts", [])
        self.raw_prompts.append(messages)
        if self.answers:
            answer = self.answers.pop(0)
        else:
            answer = self.default
        if isinstance(answer, Exception):
            raise answer
        return LLMResponse(
            text=answer, model="fake-vision", prompt_tokens=1500, completion_tokens=80
        )
