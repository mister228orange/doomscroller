"""Assembling the scoring prompt out of the editable ``scoring/*.md`` modules.

The rubric is not a Python string constant: it is a directory of markdown files that can
be edited one at a time, without touching code (see the layout below). This module is the
only place that knows how the files are combined, so a change to a rule file needs no
rebuild.

    scoring/00_system.md          role, input, boundaries
    scoring/01_axes.md            the six axes
    scoring/02_classification.md  spheres and cross-cutting tags
    scoring/03_rules.md           rules 1-15
    scoring/04_formulas.md        KPD / Eff
    scoring/05_anchors.md         anchors A1-A10
    scoring/06_procedure.md       procedure + answer format  -> goes to the user turn
    scoring/07_selfcheck.md       self-check + glossary
    scoring/08_channel_context.md trajectory rules            -> also to the user turn
    scoring/09_summary.md         channel memory task (separate call)

Why ``06`` is split out: it is the *task* (procedure + answer format), not the *rubric*.
Putting it right before the data in the user turn is more reliable than burying it at the
end of a long system message, and it keeps the system prompt stable across calls, so a
backend with prompt caching can reuse it. ``08`` stays in the system prompt: it is a rule
about how to use the input, not a step of the procedure.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .rubric import AXES

#: Modules that make up the system prompt, in order.
SYSTEM_MODULES = (
    "00_system",
    "01_axes",
    "02_classification",
    "03_rules",
    "04_formulas",
    "05_anchors",
    "07_selfcheck",
    "08_channel_context",
)

#: The procedure / answer-format module: it goes into the user turn, next to the data.
TASK_MODULE = "06_procedure"

#: The separate "update the channel memory" call.
SUMMARY_MODULE = "09_summary"

PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


class RubricError(RuntimeError):
    """The rubric directory is missing or incomplete."""


def rubric_dir_candidates() -> list[Path]:
    """Every place the rubric modules may live, in order of preference.

    1. ``SCORER_RUBRIC_DIR`` — explicit, and the only one that is a contract.
    2. ``<repo>/scoring`` — a checkout or bind mount. First among the implicit options so
       that editing a rule file takes effect immediately; a stale copy must never win.
    3. ``doomscroller/rubric`` — the wheel's own copy (``pip install doomscroller`` on a
       machine that has no checkout at all).
    4. ``./scoring`` — a container whose working directory is the checkout.

    The rule files are data, and the scorer is a separate service: refusing to start
    because a markdown directory is one level off would be a silly failure mode.
    """
    package_dir = Path(__file__).resolve().parent
    candidates: list[Path] = []
    env = (os.environ.get("SCORER_RUBRIC_DIR") or "").strip()
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(package_dir.parent.parent / "scoring")  # <repo>/scoring
    # force-include in pyproject lands `scoring/` at `doomscroller/rubric` inside the
    # wheel, i.e. one level above this module's own directory.
    candidates.append(package_dir.parent / "rubric")          # packaged inside the wheel
    candidates.append(Path.cwd() / "scoring")
    return candidates


def default_rubric_dir() -> Path:
    """The rubric directory to use.

    ``SCORER_RUBRIC_DIR`` is a contract, not a hint: if it is set but wrong, the loader
    must fail loudly rather than quietly grade with a different rubric. The implicit
    candidates, by contrast, are probed in order and the first existing one wins.
    """
    env = (os.environ.get("SCORER_RUBRIC_DIR") or "").strip()
    if env:
        return Path(env).expanduser()
    candidates = rubric_dir_candidates()[1:]  # everything except the env entry
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def load_modules(rubric_dir: str | Path | None = None) -> "RubricModules":
    """Read every module once. Raises ``RubricError`` if one is missing."""
    directory = Path(rubric_dir) if rubric_dir else default_rubric_dir()
    if not directory.is_dir():
        raise RubricError(f"rubric directory not found: {directory}")

    wanted = (*SYSTEM_MODULES, TASK_MODULE, SUMMARY_MODULE)
    texts: dict[str, str] = {}
    for name in wanted:
        path = directory / f"{name}.md"
        if not path.is_file():
            raise RubricError(
                f"missing rubric module {path.name!r} in {directory} "
                f"(expected: {', '.join(n + '.md' for n in wanted)})"
            )
        texts[name] = path.read_text(encoding="utf-8").strip()

    digest = hashlib.sha256()
    for name in wanted:
        digest.update(name.encode())
        digest.update(b"\x00")
        digest.update(texts[name].encode())
        digest.update(b"\x01")
    return RubricModules(directory=directory, texts=texts, version=digest.hexdigest()[:12])


@dataclass(frozen=True)
class RubricModules:
    directory: Path
    texts: dict[str, str]
    #: Short hash of every module. Stored with each score so an old row can be
    #: re-derived after the rubric was edited.
    version: str

    def system_text(self) -> str:
        return "\n\n".join(self.texts[name] for name in SYSTEM_MODULES)

    def task_text(self) -> str:
        return self.texts[TASK_MODULE]

    def summary_text(self) -> str:
        return self.texts[SUMMARY_MODULE]

    @property
    def anchor_text(self) -> str:
        return self.texts["05_anchors"]


@dataclass(frozen=True)
class PromptPair:
    system: str
    user: str
    version: str


def _fill(template: str, values: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        return values.get(key, match.group(0))

    return PLACEHOLDER.sub(repl, template)


def render_prompt(
    modules: RubricModules,
    *,
    text: str,
    context: Optional[str] = None,
    genre: Optional[str] = None,
    channel_state: Optional[str] = None,
    max_chars: int = 0,
) -> PromptPair:
    """Build ``(system, user)`` for one post.

    The placeholders in ``00_system.md`` are resolved to *pointers* into the data block
    rather than to copies of the data: the text lives exactly once in the prompt, in the
    user turn, where the model reads it last.
    """
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + "\n\n[... текст обрезан по лимиту ...]"

    pointers = {
        "TEXT": "<в блоке ДАННЫЕ ниже>",
        "CONTEXT": "<в блоке ДАННЫЕ ниже>" if context else "— не передан",
        "GENRE": genre or "определи сам по тексту",
        "CHANNEL_STATE": "<в блоке ДАННЫЕ ниже>" if channel_state else "— не передан",
    }
    system = _fill(modules.system_text(), pointers)

    data_block = [
        "# ДАННЫЕ",
        "",
        "TEXT:",
        "<<<",
        text.strip(),
        ">>>",
        "",
        "CONTEXT:",
        context.strip() if context else "— не передан",
        "",
        "GENRE:",
        genre or "— не передан (определи сам)",
        "",
        "CHANNEL_STATE:",
        channel_state.strip() if channel_state else "— не передан (канал оценивается впервые)",
    ]
    user = "\n".join(
        [
            modules.task_text(),
            "",
            f"# ВЕРСИЯ МЕТОДИКИ\n{modules.version}",
            "",
            "\n".join(data_block),
            "",
            "# НАПОМИНАНИЕ",
            "Верни только JSON из раздела «ФОРМАТ ОТВЕТА», без текста вокруг.",
        ]
    )
    return PromptPair(system=system, user=user, version=modules.version)


# ── channel context block ─────────────────────────────────────────────────────

def _fmt_axes(scores: dict[str, Any]) -> str:
    return " | ".join(f"{axis} {float(scores.get(axis, 0.0)):.1f}" for axis in AXES)


def render_channel_state(snapshot: dict[str, Any]) -> str:
    """Render the ``CHANNEL_STATE`` block from a state snapshot.

    Shape deliberately mirrors ``scoring/08_channel_context.md``: PROFILE / TREND /
    LOCAL_ANCHORS / TAG_DIST, plus SUMMARY and EVENTS, which are what the methodology
    calls "the trajectory, not the point".
    """
    if not snapshot:
        return ""

    lines: list[str] = []
    lines.append(f"PROFILE: {snapshot.get('profile_description') or '— (ещё не описан)'}")

    means = snapshot.get("means") or {}
    if means:
        trend = _fmt_axes(means)
        kpd = snapshot.get("kpd_mean")
        eff = snapshot.get("eff_mean")
        bits = [f"{trend} | КПД {kpd:.2f}" if kpd is not None else trend]
        if eff is not None:
            bits.append(f"Эфф {eff:.2f}")
        window = snapshot.get("window") or 5
        bits.append(f"(окно {window}, постов {snapshot.get('total_scored', 0)})")
        lines.append("TREND: " + " ".join(bits))
    else:
        lines.append("TREND: — (первый пост канала)")

    anchors = snapshot.get("local_anchors") or []
    if anchors:
        lines.append("LOCAL_ANCHORS:")
        for anchor in anchors:
            scores = anchor.get("scores") or {}
            kpd = anchor.get("kpd")
            kpd_s = f"КПД {kpd:.2f}" if isinstance(kpd, (int, float)) else "КПД —"
            tag = anchor.get("primary") or "?"
            lines.append(
                f"- {anchor.get('id')} «{anchor.get('title') or ''}» [{tag}] "
                f"{_fmt_axes(scores)} → {kpd_s}"
            )
    else:
        lines.append("LOCAL_ANCHORS: — (нет)")

    dist = snapshot.get("tag_distribution") or {}
    if dist:
        ordered = sorted(dist.items(), key=lambda kv: (-int(kv[1]), kv[0]))
        lines.append("TAG_DIST: " + ", ".join(f"{tag} {count}" for tag, count in ordered))
    else:
        lines.append("TAG_DIST: — (пусто)")

    if snapshot.get("summary"):
        lines.append(f"SUMMARY: {snapshot['summary']}")

    events = snapshot.get("events") or []
    if events:
        lines.append("EVENTS:")
        for event in events[-3:]:
            note = event.get("note") or ""
            lines.append(f"- {event.get('date')} {event.get('kind')}: {note}")
    return "\n".join(lines)
