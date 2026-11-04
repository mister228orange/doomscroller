"""Anchors as a test set: locate the real texts and re-score them.

The rubric's anchors A1–A10 are prose with numbers attached. ``LOCATOR: channel#id`` in
``scoring/05_anchors.md`` points at the actual post in the archive, which turns "the scores
we agreed on" into something that can be checked against the model actually in use:

* **locate** — which anchors exist in this archive, how long the text is;
* **calibrate** — score the anchor text again, diff every axis against the reference, and
  report where the model is systematically softer or harsher.

Calibration is a diagnostic: it never writes to ``post_scores`` and never touches a
channel's trajectory. Scoring the anchors for real is the agent's job, not this one's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from sqlmodel import Session, select

from ..models import Message
from ..subscriptions import get_subscription
from .llm import LLMError
from .prompts import RubricModules, render_prompt
from .rubric import AXES, Anchor, RubricError, extract_json, finalize

#: Same tolerance as the self-check (07, rule 6): drift beyond this is worth explaining.
TOLERANCE = 0.5


@dataclass
class AnchorCheck:
    anchor_id: str
    title: str
    genre: str
    primary: str
    channel: Optional[str]
    message_id: Optional[int]
    #: Resolution against the archive
    chat_id: Optional[int] = None
    archived: bool = False
    chars: int = 0
    note: Optional[str] = None
    #: Reference (from 05_anchors.md) and model (fresh) rows
    reference: dict[str, float] = field(default_factory=dict)
    reference_kpd: Optional[float] = None
    model: dict[str, float] = field(default_factory=dict)
    model_kpd: Optional[float] = None
    deltas: dict[str, float] = field(default_factory=dict)
    verdict: str = "unknown"  # ok | drift | unscored | missing
    error: Optional[str] = None

    @property
    def worst_delta(self) -> float:
        return max((abs(value) for value in self.deltas.values()), default=0.0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "anchor": self.anchor_id,
            "title": self.title,
            "genre": self.genre,
            "primary": self.primary,
            "locator": f"{self.channel}#{self.message_id}" if self.channel else None,
            "archived": self.archived,
            "chars": self.chars,
            "reference": self.reference,
            "reference_kpd": self.reference_kpd,
            "model_scores": self.model or None,
            "model_kpd": self.model_kpd,
            "deltas": self.deltas or None,
            "worst_delta": round(self.worst_delta, 2) if self.deltas else None,
            "verdict": self.verdict,
            "note": self.note,
            "error": self.error,
        }


def resolve_anchor(session: Session, anchor: Anchor) -> tuple[Optional[int], Optional[Message]]:
    """Find the text behind a locator. ``(chat_id, message)``; message is None if absent."""
    if not anchor.channel or anchor.message_id is None:
        return None, None
    sub = get_subscription(session, anchor.channel)
    if sub is None or sub.chat_id is None:
        return None, None
    message = session.exec(
        select(Message).where(
            Message.chat_id == int(sub.chat_id),
            Message.message_id == int(anchor.message_id),
        )
    ).first()
    return int(sub.chat_id), message


def locate(session: Session, anchors: dict[str, Anchor]) -> list[AnchorCheck]:
    """Which anchors can actually be inspected here — no model calls, no scoring."""
    checks: list[AnchorCheck] = []
    for anchor_id in sorted(anchors, key=lambda value: int(value.lstrip("A") or 0)):
        anchor = anchors[anchor_id]
        check = AnchorCheck(
            anchor_id=anchor.id,
            title=anchor.title,
            genre=anchor.genre,
            primary=anchor.primary,
            channel=anchor.channel,
            message_id=anchor.message_id,
            reference=dict(anchor.scores),
            reference_kpd=anchor.kpd,
        )
        if not anchor.channel:
            check.verdict = "missing"
            check.note = "нет LOCATOR — якорь существует только как описание"
        else:
            chat_id, message = resolve_anchor(session, anchor)
            check.chat_id = chat_id
            if chat_id is None:
                check.verdict = "missing"
                check.note = f"канал {anchor.channel!r} не подписан"
            elif message is None:
                check.verdict = "missing"
                check.note = "сообщение не найдено в архиве (нужен poll)"
            else:
                check.archived = True
                check.chars = len(message.text or "")
        checks.append(check)
    return checks


async def calibrate(
    anchors: dict[str, Anchor],
    *,
    session_factory,
    llm,
    modules: RubricModules,
    max_chars: int = 12000,
    only: Optional[list[str]] = None,
) -> list[AnchorCheck]:
    """Score every archived anchor and diff it against the reference numbers.

    One model call per anchor. The prompt is the *same* rubric with no ``CHANNEL_STATE``:
    an anchor is a global reference, so it must be judged the way the methodology says —
    by absolute anchors, not by a channel's trend.
    """
    wanted = {value.upper() for value in only} if only else None
    with session_factory() as session:
        checks = locate(session, anchors)
        texts: dict[str, str] = {}
        for check in checks:
            if check.archived and check.chat_id is not None and check.message_id is not None:
                _, message = resolve_anchor(session, anchors[check.anchor_id])
                texts[check.anchor_id] = (message.text if message is not None else "") or ""

    for check in checks:
        if wanted is not None and check.anchor_id not in wanted:
            continue
        if not check.archived:
            continue
        text = texts.get(check.anchor_id, "")
        if not text.strip():
            check.verdict = "missing"
            check.note = "пустой текст"
            continue

        prompt = render_prompt(modules, text=text, max_chars=max_chars)
        try:
            response = await llm.chat(system=prompt.system, user=prompt.user)
            rubric = finalize(extract_json(response.text), anchors=anchors)
        except LLMError as exc:
            check.verdict = "unscored"
            check.error = f"{type(exc).__name__}: {exc}"
            if not exc.retryable:
                # Bad key or empty balance: the remaining anchors would fail identically.
                raise
            continue
        except RubricError as exc:
            check.verdict = "unscored"
            check.error = f"{type(exc).__name__}: {exc}"
            continue

        check.model = dict(rubric.scores)
        check.model_kpd = rubric.kpd
        # Clamps are part of the diagnosis: if the rubric caps an axis below the reference
        # (A2 «Фёрс» is a "трактат" with I=9.5, while rule 12 caps трактат at 9.0), the
        # calibration is the place where that inconsistency surfaces.
        if rubric.violations:
            check.note = "; ".join(rubric.violations)
        check.deltas = {
            axis: round(rubric.scores[axis] - anchor_value, 2)
            for axis, anchor_value in check.reference.items()
        }
        check.verdict = "ok" if check.worst_delta <= TOLERANCE else "drift"
    return checks


def axis_summary(checks: list[AnchorCheck]) -> dict[str, dict[str, float]]:
    """Mean signed and absolute delta per axis — where the model leans, across anchors."""
    per_axis: dict[str, list[float]] = {axis: [] for axis in AXES}
    for check in checks:
        for axis, delta in check.deltas.items():
            if axis in per_axis:
                per_axis[axis].append(delta)
    summary: dict[str, dict[str, float]] = {}
    for axis, values in per_axis.items():
        if not values:
            continue
        summary[axis] = {
            "mean": round(sum(values) / len(values), 2),
            "mean_abs": round(sum(abs(value) for value in values) / len(values), 2),
            "n": len(values),
        }
    return summary
