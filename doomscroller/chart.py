"""Channel rating visualization.

Two renderers over the same data:

* ``render_html`` — a single self-contained HTML file: inline SVG, no JavaScript, no
  external assets, so it opens anywhere and survives being emailed.
* ``render_ascii`` — a sparkline for the terminal.

The trend line is recomputed exactly the way :mod:`doomscroller.scoring.state` folds scores
in (incremental mean with the channel's window), so the picture agrees with the stored
trajectory instead of inventing its own smoothing.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any, Optional

from sqlmodel import Session, select

from .models import ChannelEvent, ChannelState, PostScore
from .scoring.state import AXES

#: Sphere → color. Muted enough to read in both light and dark browser themes.
SPHERE_COLORS: dict[str, str] = {
    "TECH": "#3b82f6",
    "SCI": "#14b8a6",
    "POWER": "#ef4444",
    "ECON": "#22c55e",
    "SOC": "#f97316",
    "CULT": "#a855f7",
    "PERS": "#ec4899",
}
FALLBACK_COLOR = "#94a3b8"
AXIS_LABELS_RU: dict[str, str] = {
    "I": "Интересность",
    "V": "Ценность",
    "U": "Уникальность",
    "P": "Порог входа",
    "S": "Смешность",
    "D": "Плотность",
}
SPARK = "▁▂▃▄▅▆▇█"


def _color(primary: Optional[str]) -> str:
    return SPHERE_COLORS.get(primary or "", FALLBACK_COLOR)


def _ewma_series(kpds: list[float], window: int) -> list[float]:
    """The trend the trajectory would have shown, replayed post by post."""
    means: list[float] = []
    mean: Optional[float] = None
    for n, value in enumerate(kpds, start=1):
        if mean is None:
            mean = value
        else:
            mean = mean + (value - mean) / min(n, window)
        means.append(mean)
    return means


def collect_chart_data(session: Session, chat_id: int) -> dict[str, Any]:
    """Everything the renderers need, in message order."""
    state = session.exec(
        select(ChannelState).where(ChannelState.chat_id == chat_id)
    ).first()
    rows = list(
        session.exec(
            select(PostScore)
            .where(PostScore.chat_id == chat_id, PostScore.status == "scored")
            .order_by(PostScore.message_id.asc())
        ).all()
    )
    events = list(
        session.exec(
            select(ChannelEvent)
            .where(ChannelEvent.chat_id == chat_id)
            .order_by(ChannelEvent.id.desc())
            .limit(30)
        ).all()
    )
    posts = [
        {
            "message_id": row.message_id,
            "kpd": float(row.kpd or 0.0),
            "primary": row.primary,
            "secondary": list(row.secondary or []),
            "title": row.title or "",
            "axes": {axis: float(getattr(row, f"score_{axis.lower()}") or 0.0) for axis in AXES},
        }
        for row in rows
    ]
    primary_counts: dict[str, int] = {}
    secondary_counts: dict[str, int] = {}
    for row in rows:
        if row.primary:
            primary_counts[row.primary] = primary_counts.get(row.primary, 0) + 1
        for tag in row.secondary or []:
            secondary_counts[tag] = secondary_counts.get(tag, 0) + 1
    return {
        "chat_id": chat_id,
        "chat_ref": (state.chat_ref if state else None) or str(chat_id),
        "title": state.title if state else None,
        "profile": state.profile_description if state else None,
        "summary": state.summary if state else None,
        "window": int(state.window) if state else 5,
        "kpd_mean": float(state.kpd_mean) if state and state.kpd_mean is not None else None,
        "counters": {
            "scored": int(state.total_scored) if state else 0,
            "skipped": int(state.total_skipped) if state else 0,
            "failed": int(state.total_failed) if state else 0,
        },
        "models": sorted({row.model for row in rows if row.model}),
        "posts": posts,
        "primary_counts": primary_counts,
        "secondary_counts": secondary_counts,
        "events": [
            {
                "kind": event.kind,
                "message_id": event.message_id,
                "note": event.note,
                "created_at": event.created_at.isoformat() if event.created_at else None,
            }
            for event in events
        ],
    }


def _kpd_chart_svg(posts: list[dict[str, Any]], window: int, mean: Optional[float]) -> str:
    """One bar per scored post, colored by sphere, with the trend and mean lines."""
    if not posts:
        return "<p>Нет оценённых постов.</p>"
    width, height, pad = 1180, 300, 34
    plot_w, plot_h = width - 2 * pad, height - 2 * pad - 18
    step = max(4.0, min(30.0, plot_w / len(posts)))
    bars, labels = [], []
    kpds = [post["kpd"] for post in posts]
    trend = _ewma_series(kpds, window)

    def y(value: float) -> float:
        return pad + plot_h * (1 - max(0.0, min(10.0, value)) / 10.0)

    x = pad
    for index, post in enumerate(posts):
        bar_w = step * 0.72
        top = y(post["kpd"])
        bars.append(
            f'<rect x="{x:.1f}" y="{top:.1f}" width="{bar_w:.1f}" height="{pad + plot_h - top:.1f}" '
            f'fill="{_color(post["primary"])}" rx="1.5"><title>#{post["message_id"]} · КПД '
            f'{post["kpd"]:.2f} · {html.escape(post["title"][:80])}</title></rect>'
        )
        if index % max(1, len(posts) // 14) == 0:
            labels.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{height - 6}" font-size="10" fill="#888" '
                f'text-anchor="middle">#{post["message_id"]}</text>'
            )
        x += step

    def polyline(values: list[float], color: str, dash: str = "") -> str:
        pts = []
        x = pad
        for value in values:
            pts.append(f"{x + step * 0.36:.1f},{y(value):.1f}")
            x += step
        return (
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" '
            f'stroke-width="2"{dash}/>'
        )

    grid = "".join(
        f'<line x1="{pad}" y1="{y(v):.1f}" x2="{pad + plot_w}" y2="{y(v):.1f}" '
        f'stroke="#e2e8f0" stroke-width="1"/>'
        f'<text x="{pad - 6}" y="{y(v) + 4:.1f}" font-size="10" fill="#888" text-anchor="end">{v}</text>'
        for v in range(0, 11, 2)
    )
    mean_line = (
        polyline([mean] * len(posts), "#f59e0b", ' stroke-dasharray="6 4"')
        if mean is not None
        else ""
    )
    legend = "".join(
        f'<span style="color:{color}">■</span> {name} '
        for name, color in sorted(SPHERE_COLORS.items())
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="КПД по постам" style="width:100%;height:auto;background:#fff">'
        f'{grid}{"".join(bars)}{polyline(trend, "#334155")}{mean_line}{"".join(labels)}'
        f"</svg>"
        f'<p style="font-size:12px;color:#666">{legend} — сферы; тёмная линия — тренд '
        f'(окно {window}); жёлтый пунктир — средний КПД{f" {mean:.2f}" if mean is not None else ""}.</p>'
    )


def _hbars_svg(counts: dict[str, int], colors: Optional[dict[str, str]] = None) -> str:
    if not counts:
        return "<p>—</p>"
    total = max(counts.values())
    rows = []
    for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        color = (colors or {}).get(name, FALLBACK_COLOR)
        pct = 100.0 * count / total
        rows.append(
            f'<div style="display:flex;align-items:center;gap:8px;margin:3px 0">'
            f'<span style="width:64px;font-size:13px">{html.escape(name)}</span>'
            f'<div style="flex:1;background:#f1f5f9;border-radius:3px">'
            f'<div style="width:{pct:.0f}%;background:{color};height:14px;border-radius:3px"></div></div>'
            f'<span style="width:28px;font-size:12px;color:#555;text-align:right">{count}</span></div>'
        )
    return f"<div>{''.join(rows)}</div>"


def _axis_svg(axes: dict[str, float]) -> str:
    bars = []
    for axis in AXES:
        value = axes.get(axis, 0.0)
        pct = 10.0 * max(0.0, min(10.0, value))
        bars.append(
            f'<div style="display:flex;align-items:center;gap:8px;margin:4px 0">'
            f'<span style="width:150px;font-size:13px">{AXIS_LABELS_RU[axis]} '
            f'<b style="color:#334155">{axis}</b></span>'
            f'<div style="flex:1;background:#f1f5f9;border-radius:3px">'
            f'<div style="width:{pct:.0f}%;background:#334155;height:16px;border-radius:3px"></div></div>'
            f'<span style="width:44px;font-size:13px;color:#334155;text-align:right">{value:.2f}</span></div>'
        )
    return f"<div>{''.join(bars)}</div>"


def _post_table(posts: list[dict[str, Any]], chat_ref: str) -> str:
    if not posts:
        return "<p>—</p>"
    rows = []
    for post in posts:
        link = f'https://t.me/{chat_ref.lstrip("@")}/{post["message_id"]}'
        tags = " ".join(
            f'<span style="background:#eef2f7;border-radius:3px;padding:0 5px;font-size:11px">{html.escape(tag)}</span>'
            for tag in ([post["primary"]] + post["secondary"])
            if tag
        )
        rows.append(
            f'<tr><td style="padding:3px 8px;white-space:nowrap"><a href="{link}">#{post["message_id"]}</a></td>'
            f'<td style="padding:3px 8px;font-weight:600">{post["kpd"]:.2f}</td>'
            f'<td style="padding:3px 8px">{tags}</td>'
            f'<td style="padding:3px 8px;color:#444">{html.escape(post["title"][:90])}</td></tr>'
        )
    return (
        '<table style="border-collapse:collapse;width:100%;font-size:13px">'
        + "".join(rows)
        + "</table>"
    )


def render_html(data: dict[str, Any]) -> str:
    """One self-contained HTML page: inline SVG only, no JS, no external assets."""
    posts = data["posts"]
    kpds = [post["kpd"] for post in posts]
    axis_means = {
        axis: (sum(post["axes"][axis] for post in posts) / len(posts)) if posts else 0.0
        for axis in AXES
    }
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    counters = data["counters"]
    models = ", ".join(data["models"]) or "—"
    events = "".join(
        f'<li style="margin:2px 0"><code>{html.escape(str(item["created_at"]))[:19]}</code> '
        f'<b>{html.escape(item["kind"])}</b> — {html.escape(item["note"] or "")}</li>'
        for item in data["events"][:12]
    )
    top = sorted(posts, key=lambda post: -post["kpd"])[:8]
    bottom = sorted(posts, key=lambda post: post["kpd"])[:4]
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Рейтинг канала {html.escape(data["chat_ref"])}</title></head>
<body style="font-family:system-ui,sans-serif;max-width:1240px;margin:24px auto;padding:0 16px;color:#1e293b">
<h1 style="margin-bottom:2px">Рейтинг канала {html.escape(data["chat_ref"])}</h1>
<p style="color:#64748b;margin-top:0">{data["title"] or ""}</p>
<p style="font-size:13px;color:#64748b">оценено {counters["scored"]}, пропущено {counters["skipped"]},
отказов {counters["failed"]} · модели: {html.escape(models)} · средний КПД
{data["kpd_mean"] if data["kpd_mean"] is not None else "—"} · максимум {max(kpds) if kpds else "—"} ·
собрано {generated}</p>
<blockquote style="border-left:3px solid #cbd5e1;margin:8px 0;padding:2px 12px;color:#475569">
{html.escape(data["profile"] or "")}</blockquote>
<blockquote style="border-left:3px solid #94a3b8;margin:8px 0;padding:2px 12px;color:#334155">
{html.escape(data["summary"] or "")}</blockquote>
<h2>КПД по постам</h2>
{_kpd_chart_svg(posts, data["window"], data["kpd_mean"])}
<h2>Средние по осям</h2>
{_axis_svg(axis_means)}
<div style="display:flex;gap:48px;flex-wrap:wrap">
<div><h2>Сферы (primary)</h2>{_hbars_svg(data["primary_counts"], SPHERE_COLORS)}</div>
<div><h2>Темы (secondary)</h2>{_hbars_svg(data["secondary_counts"])}</div>
</div>
<div style="display:flex;gap:48px;flex-wrap:wrap">
<div style="flex:1;min-width:420px"><h2>Лучшие посты</h2>{_post_table(top, data["chat_ref"])}</div>
<div style="flex:1;min-width:420px"><h2>Худшие посты</h2>{_post_table(bottom, data["chat_ref"])}</div>
</div>
<h2>События траектории</h2><ul style="font-size:13px">{events or "<li>—</li>"}</ul>
<p style="color:#94a3b8;font-size:12px">doomscroller · rubric {html.escape(str(data.get("window")))}</p>
</body></html>"""


def render_ascii(data: dict[str, Any]) -> str:
    """Terminal view: one sparkline character per scored post, oldest first."""
    posts = data["posts"]
    lines: list[str] = []
    header = f"{data['chat_ref']} — {data['counters']['scored']} оценено, средний КПД {data['kpd_mean']}"
    lines.append(header)
    if posts:
        def char(value: float) -> str:
            return SPARK[min(7, max(0, int(round(value)) - 1))] if value > 0 else SPARK[0]

        lines.append("КПД: " + "".join(char(post["kpd"]) for post in posts))
        lines.append(f"шкала: {SPARK[0]}=1 … {SPARK[-1]}=8+, порядок слева направо — старые посты первыми")
        top = sorted(posts, key=lambda post: -post["kpd"])[:5]
        lines.append("топ:")
        for post in top:
            lines.append(f"  {post['kpd']:.2f}  #{post['message_id']} [{post['primary']}] {post['title'][:60]}")
    return "\n".join(lines)
