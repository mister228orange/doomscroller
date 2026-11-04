"""Rubric result parsing, validation and the self-check filter.

The model returns JSON; this module decides whether that JSON is *usable*. Nothing here
calls a model, so all of it is testable without a network.

The important asymmetry: the model proposes scores, but **derived values are recomputed
here** (KPD, Eff), and the caps from ``scoring/07_selfcheck.md`` are enforced
mechanically. A model that returns ``KPD: 9.99`` for axes that average 6.0 gets corrected,
and the correction is recorded in ``violations`` instead of being silently swallowed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

#: Axes, in the order the rubric presents them.
AXES = ("I", "V", "U", "P", "S", "D")

PRIMARY_SPHERES = ("TECH", "POWER", "ECON", "SOC", "CULT", "SCI", "PERS")

SECONDARY_TAGS = (
    "DEGR",
    "HIER",
    "HYPOC",
    "IDENT",
    "FUT",
    "TRUST",
    "BODY",
    "CONFL",
)

DENSITY_TYPES = ("concentration", "saturation")
CONFIDENCE_LEVELS = ("high", "medium", "low")

#: Rule 12 — genre ceiling on I.
GENRE_I_CAP = {
    "свидетельство": 9.5,
    "полемика": 9.0,
    "трактат": 9.0,
    "памфлет": 9.0,
    "киногид": 9.0,
    "прогноз": 9.0,
    "мемуар": 8.5,
    "мемуар-памфлет": 8.5,
    "фид-анонс": 7.0,
    "фид-позиция": 9.0,
}

#: Rule 10 markers: "legitimises" instead of "names a new correlation". The lookbehind
#: keeps "не разрешает" (the honest reading) from triggering the cap.
_LEGITIMISING = re.compile(r"(?<!не )(разреша|легитимиз|оправдыва)")

#: Self-check 6: drift from the nearest anchor above this = low confidence.
ANCHOR_DRIFT_LIMIT = 0.5

#: Rule 7: below this KPD, Eff is not defined.
EFF_MIN_KPD = 5.0


class RubricError(ValueError):
    """The model's answer cannot be turned into a rubric row."""


@dataclass(frozen=True)
class Anchor:
    id: str
    title: str
    genre: str
    primary: str
    scores: dict[str, float]
    kpd: float
    eff: Optional[float]
    #: Where the real text lives, when the anchor file says so
    #: (``LOCATOR: design4TOR#44``). Not part of the methodology — only a pointer so the
    #: anchor can be re-scored from the archive.
    channel: Optional[str] = None
    message_id: Optional[int] = None


def parse_anchors(text: str) -> dict[str, Anchor]:
    """Parse ``scoring/05_anchors.md`` into anchor rows.

    Tolerant by design: the anchor file is prose meant for a human, and this only needs
    the ID, the axis line and the KPD/Eff line. Anything unparseable is skipped rather
    than raising — the rubric must keep working while the file is being edited.
    """
    anchors: dict[str, Anchor] = {}
    current: Optional[dict[str, Any]] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        heading = re.match(r"^#{2,3}\s*(A\d+)\s*(.*)$", line)
        if heading:
            rest = heading.group(2).strip()
            genre_match = re.search(r"\(([^)]*)\)\s*$", rest)
            current = {
                "id": heading.group(1),
                "title": (
                    (rest[: genre_match.start()] if genre_match else rest)
                    .replace("«", "")
                    .replace("»", "")
                    .strip()
                ),
                "scores": {},
                "kpd": 0.0,
                "eff": None,
                "genre": genre_match.group(1).strip() if genre_match else "",
                "primary": "",
            }
            anchors[current["id"]] = current  # type: ignore[assignment]
            continue
        if current is None:
            continue

        axes_line = re.search(
            r"I\s*([\d.]+)\s*\|\s*V\s*([\d.]+)\s*\|\s*U\s*([\d.]+)\s*\|\s*"
            r"P\s*([\d.]+)\s*\|\s*S\s*([\d.]+)\s*\|\s*D\s*([\d.]+)",
            line,
        )
        if axes_line:
            current["scores"] = {
                axis: float(value) for axis, value in zip(AXES, axes_line.groups())
            }
            tail = re.search(r"→\s*([\d.]+)\s*/\s*(—|[\d.]+)", line)
            if tail:
                current["kpd"] = float(tail.group(1))
                current["eff"] = None if tail.group(2) == "—" else float(tail.group(2))
            continue

        # The anchor file sometimes uses a sphere name as a secondary tag (A2 "TECH",
        # A5 "POWER"); only the primary needs to be a real sphere, the rest is display.
        locator = re.match(r"^LOCATOR:\s*([^\s#]+)#(\d+)", line, re.IGNORECASE)
        if locator:
            current["channel"] = locator.group(1)
            current["message_id"] = int(locator.group(2))
            continue

        tagging = re.match(r"^([A-Z]+)(?:\s*\+\s*\[([A-Z, ]+)\])?$", line)
        if tagging and tagging.group(1) in PRIMARY_SPHERES:
            current["primary"] = tagging.group(1)
            continue

        genre = re.match(r"^\((.*)\)$", line)
        if genre and not current["genre"]:
            current["genre"] = genre.group(1).strip()

    return {aid: Anchor(**data) for aid, data in anchors.items() if data["scores"]}


# ── JSON extraction ───────────────────────────────────────────────────────────

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model answer.

    Models wrap JSON in fences, prepend prose ("## Разбор ..."), or append a markdown
    table — the answer format in ``06_procedure.md`` explicitly asks for all three. So a
    bare ``json.loads`` is not enough.
    """
    raw = (text or "").strip()
    if not raw:
        raise RubricError("empty model answer")

    candidates: list[str] = [raw]
    candidates.extend(match.group(1) for match in _FENCE.finditer(raw))

    decoder = json.JSONDecoder()
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        # Scan for the first balanced object in the text.
        for start in [i for i, char in enumerate(candidate) if char == "{"][:20]:
            try:
                parsed, _ = decoder.raw_decode(candidate[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    raise RubricError("no JSON object found in model answer")


# ── the rubric row ────────────────────────────────────────────────────────────

def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", ".").strip())
        except ValueError:
            return default
    return default


def _clamp(value: float, low: float = 0.0, high: float = 10.0) -> float:
    return max(low, min(high, value))


def compute_kpd(scores: dict[str, float]) -> float:
    """(I + V + U + D) / 4 — recomputed, never trusted from the model."""
    return round(sum(scores[axis] for axis in ("I", "V", "U", "D")) / 4.0, 2)


def compute_eff(kpd: float, p: float) -> Optional[float]:
    """KPD / max(P, 1), defined only above KPD 5.0 (rule 7)."""
    if kpd <= EFF_MIN_KPD:
        return None
    return round(kpd / max(p, 1.0), 2)


@dataclass
class Rubric:
    genre: str
    primary: str
    secondary: list[str]
    main_thesis: str
    clauses: dict[str, int]
    density_type: str
    flags: list[str]
    scores: dict[str, float]
    justification: dict[str, str]
    kpd: float
    eff: Optional[float]
    nearest_anchors: list[str]
    confidence: str
    violations: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def axis_columns(self) -> dict[str, float]:
        return {f"score_{axis.lower()}": self.scores[axis] for axis in AXES}


def _normalise_flags(raw_flags: Any) -> list[str]:
    if raw_flags is None:
        return []
    if isinstance(raw_flags, str):
        raw_flags = [raw_flags]
    out: list[str] = []
    for item in raw_flags:
        if not isinstance(item, str):
            continue
        item = item.strip()
        if item and item.lower() not in {"null", "none", "нет"}:
            out.append(item)
    return out


def _normalise_clauses(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key in ("total", "fact", "arg", "slogan"):
        value = _to_float(raw.get(key))
        if value is not None:
            out[key] = int(value)
    return out


def _anchor_drift(scores: dict[str, float], anchor_ids: list[str], anchors: dict[str, Anchor]) -> Optional[float]:
    """Smallest max-axis distance between the row and any of the named anchors."""
    best: Optional[float] = None
    for anchor_id in anchor_ids:
        anchor = anchors.get(anchor_id)
        if anchor is None or not anchor.scores:
            continue
        distance = max(abs(scores[axis] - anchor.scores.get(axis, scores[axis])) for axis in AXES)
        best = distance if best is None else min(best, distance)
    return best


def finalize(
    raw: dict[str, Any],
    *,
    anchors: Optional[dict[str, Anchor]] = None,
    genre_hint: Optional[str] = None,
) -> Rubric:
    """Validate one model answer against the rubric and apply the self-check.

    Raises ``RubricError`` when the answer is not a scoring at all (no axes, unknown
    sphere). Everything recoverable is recovered and recorded in ``violations``.
    """
    violations: list[str] = []
    anchors = anchors or {}

    raw_scores = raw.get("scores")
    if not isinstance(raw_scores, dict):
        # Some models flatten the axes to the top level.
        raw_scores = {axis: raw.get(axis) for axis in AXES}

    scores: dict[str, float] = {}
    for axis in AXES:
        value = _to_float(raw_scores.get(axis))
        if value is None:
            raise RubricError(f"missing or non-numeric score for axis {axis!r}")
        if value < 0.0 or value > 10.0:
            violations.append(f"score {axis} out of range ({value:+.1f}) -> clamped")
        scores[axis] = round(_clamp(value), 1)

    primary = str(raw.get("primary") or "").strip().upper()
    if primary not in PRIMARY_SPHERES:
        raise RubricError(
            f"primary must be one of {', '.join(PRIMARY_SPHERES)}; got {primary!r}"
        )

    raw_secondary = raw.get("secondary") or []
    if isinstance(raw_secondary, str):
        raw_secondary = [part for part in re.split(r"[,\s]+", raw_secondary) if part]
    secondary: list[str] = []
    for tag in raw_secondary:
        if not isinstance(tag, str):
            continue
        tag = tag.strip().upper()
        if tag in SECONDARY_TAGS and tag not in secondary:
            secondary.append(tag)
        elif tag:
            violations.append(f"unknown secondary tag {tag!r} dropped")
    if len(secondary) > 2:
        violations.append(f"secondary truncated {secondary} -> {secondary[:2]}")
        secondary = secondary[:2]

    genre_raw = str(raw.get("genre") or "").strip()
    genre = genre_raw.lower()
    caps = []
    for candidate in (genre, (genre_hint or "").strip().lower()):
        cap = GENRE_I_CAP.get(candidate)
        if cap is not None:
            caps.append(cap)
    if caps and scores["I"] > min(caps):
        violations.append(
            f"genre ceiling: I {scores['I']:.1f} -> {min(caps):.1f} for genre {genre or genre_hint!r}"
        )
        scores["I"] = round(min(caps), 1)

    flags = _normalise_flags(raw.get("flags"))
    joined_flags = " ".join(flags).lower()

    # Rule 9 / self-check 3: folklore carries no thesis, so D is zero by definition.
    if "фольклор" in joined_flags and scores["D"] != 0.0:
        violations.append(f"folklore: D {scores['D']:.1f} -> 0.0")
        scores["D"] = 0.0

    # Rule 10 / self-check 2: a text that legitimises is not a new correlation.
    justification_raw = raw.get("justification")
    justification: dict[str, str] = {}
    if isinstance(justification_raw, dict):
        for axis in AXES:
            value = justification_raw.get(axis)
            if isinstance(value, str) and value.strip():
                justification[axis] = value.strip()[:600]
    legitimising_prose = f"{joined_flags} {justification.get('V', '')}".lower()
    if _LEGITIMISING.search(legitimising_prose) and scores["V"] > 4.0:
        violations.append(f"rule 10: 'legitimises' -> V {scores['V']:.1f} capped at 4.0")
        scores["V"] = 4.0

    kpd = compute_kpd(scores)
    eff = compute_eff(kpd, scores["P"])

    raw_anchors = raw.get("nearest_anchors") or []
    if isinstance(raw_anchors, str):
        raw_anchors = [part.strip() for part in raw_anchors.replace(",", " ").split()]
    nearest_anchors = [
        str(item).strip().upper()
        for item in raw_anchors
        if isinstance(item, (str, int)) and str(item).strip()
    ][:4]

    confidence = str(raw.get("confidence") or "medium").strip().lower()
    if confidence not in CONFIDENCE_LEVELS:
        violations.append(f"confidence {confidence!r} -> medium")
        confidence = "medium"

    drift = _anchor_drift(scores, nearest_anchors, anchors)
    if drift is not None and drift > ANCHOR_DRIFT_LIMIT and confidence != "low":
        violations.append(
            f"self-check 6: drift {drift:.2f} from nearest anchor -> confidence low"
        )
        confidence = "low"

    density_type = str(raw.get("density_type") or "").strip().lower()
    if density_type not in DENSITY_TYPES:
        density_type = "concentration"
        violations.append(f"density_type defaulted to {density_type!r}")

    thesis = str(raw.get("main_thesis") or "").strip()
    if not thesis:
        violations.append("empty main_thesis")
        thesis = "(тезис не сформулирован моделью)"
    thesis = thesis[:600]

    return Rubric(
        genre=genre_raw,
        primary=primary,
        secondary=secondary,
        main_thesis=thesis,
        clauses=_normalise_clauses(raw.get("clauses")),
        density_type=density_type,
        flags=flags,
        scores=scores,
        justification=justification,
        kpd=kpd,
        eff=eff,
        nearest_anchors=nearest_anchors,
        confidence=confidence,
        violations=violations,
        raw=raw,
    )
