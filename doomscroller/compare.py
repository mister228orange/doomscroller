"""Side-by-side comparison of scored channels.

One table, channels as columns, metrics as rows: volume, KPD spread, the six axis means,
the spheres each channel lives in, and its best post. Plain averages over all scored rows —
not the EWMA trend — so channels of different sizes are compared like with like.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlmodel import Session, select

from .models import ChannelState, PostScore
from .scoring.rubric import AXES
from .scoring.state import pending_counts


def _avg(rows: list[PostScore], attr: str) -> Optional[float]:
    values = [float(getattr(row, attr) or 0.0) for row in rows]
    return round(sum(values) / len(values), 2) if values else None


def collect_comparison(session: Session, chat_refs: list[str]) -> list[dict[str, Any]]:
    """One dict per channel, in the order the caller listed them."""
    states = {
        state.chat_ref: state
        for state in session.exec(select(ChannelState)).all()
        if state.chat_ref
    }
    out: list[dict[str, Any]] = []
    for ref in chat_refs:
        state = states.get(ref.lstrip("@"))
        if state is None:
            out.append({"chat": ref, "missing": True})
            continue
        chat_id = int(state.chat_id)
        rows = list(
            session.exec(
                select(PostScore).where(
                    PostScore.chat_id == chat_id, PostScore.status == "scored"
                )
            ).all()
        )
        kpds = sorted(float(row.kpd or 0.0) for row in rows)
        spheres: dict[str, int] = {}
        for row in rows:
            if row.primary:
                spheres[row.primary] = spheres.get(row.primary, 0) + 1
        top_spheres = sorted(spheres.items(), key=lambda item: (-item[1], item[0]))[:3]
        best = max(rows, key=lambda row: float(row.kpd or 0.0), default=None)
        out.append(
            {
                "chat": state.chat_ref,
                "title": state.title,
                "scored": len(rows),
                "skipped": int(state.total_skipped or 0),
                "failed": int(state.total_failed or 0),
                "pending": pending_counts(session, [chat_id]).get(chat_id, 0),
                "kpd_mean": round(sum(kpds) / len(kpds), 2) if kpds else None,
                "kpd_median": round(kpds[len(kpds) // 2], 2) if kpds else None,
                "kpd_max": kpds[-1] if kpds else None,
                "kpd_min": kpds[0] if kpds else None,
                "axes": {axis: _avg(rows, f"score_{axis.lower()}") for axis in AXES},
                "top_spheres": [f"{name} {count}" for name, count in top_spheres],
                "models": sorted({row.model for row in rows if row.model}),
                "drift": state.drift_note,
                "best": (
                    {
                        "message_id": best.message_id,
                        "kpd": float(best.kpd or 0.0),
                        "title": (best.title or "")[:70],
                        "primary": best.primary,
                    }
                    if best
                    else None
                ),
            }
        )
    return out


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def render_table(data: list[dict[str, Any]]) -> str:
    """Metrics as rows, channels as columns — the shape that reads side by side."""
    chats = [item["chat"] for item in data]
    width = max(len(chat) for chat in chats) + 2
    lines = ["", " | ".join(["метрика".ljust(10)] + [chat.ljust(width - 2) for chat in chats])]
    lines.append("-" * (12 + sum(len(chat) + 3 for chat in chats)))

    def row(label: str, values: list[Any]) -> None:
        lines.append(
            " | ".join([label.ljust(10)] + [_fmt(value).ljust(width - 2) for value in values])
        )

    row("оценено", [item.get("scored") for item in data])
    row("пропущено", [item.get("skipped") for item in data])
    row("в очереди", [item.get("pending") for item in data])
    row("отказов", [item.get("failed") for item in data])
    row("КПД средн", [item.get("kpd_mean") for item in data])
    row("КПД медиан", [item.get("kpd_median") for item in data])
    row("КПД макс", [item.get("kpd_max") for item in data])
    row("КПД мин", [item.get("kpd_min") for item in data])
    for axis, label in (("I", "Инт"), ("V", "Ценн"), ("U", "Уник"), ("P", "Порог"), ("S", "Смеш"), ("D", "Плот")):
        row(f"{label} {axis}", [item.get("axes", {}).get(axis) for item in data])
    row("сферы", [", ".join(item.get("top_spheres", [])) for item in data])
    row("модели", [", ".join(item.get("models", [])) for item in data])
    lines.append("")
    for item in data:
        best = item.get("best")
        if best:
            lines.append(
                f"лучший {item['chat']}: КПД {best['kpd']:.2f} #{best['message_id']} "
                f"[{best['primary']}] {best['title']}"
            )
        if item.get("drift"):
            lines.append(f"дрейф {item['chat']}: {item['drift']}")
    lines.append("")
    lines.append(
        "средние — простые по всем оценённым постам; channel-state показывает текущий "
        "уровень EWMA-тренда (окно 5), это другое число"
    )
    return "\n".join(lines)
