"""Rubric validation: the arithmetic, the caps, and the self-check filter."""

from __future__ import annotations

import json

import pytest

from doomscroller.scoring.prompts import load_modules
from doomscroller.scoring.rubric import (
    ANCHOR_DRIFT_LIMIT,
    RubricError,
    compute_eff,
    compute_kpd,
    extract_json,
    finalize,
    parse_anchors,
)

from fakes import score_answer


@pytest.fixture(scope="module")
def anchors():
    return parse_anchors(load_modules().anchor_text)


def test_live_anchor_file_parses_into_all_ten():
    anchors = parse_anchors(load_modules().anchor_text)
    assert sorted(anchors, key=lambda a: int(a[1:])) == [f"A{n}" for n in range(1, 11)]
    a1 = anchors["A1"]
    assert a1.primary == "POWER"
    assert a1.scores["U"] == 10.0
    assert a1.kpd == 9.75
    assert a1.eff == 1.95
    # A9/A10 are below the KPD floor, so Eff is undefined rather than zero
    assert anchors["A9"].eff is None


def test_kpd_uses_only_the_four_substantive_axes():
    scores = {"I": 9.0, "V": 8.0, "U": 7.0, "P": 2.0, "S": 1.0, "D": 8.0}
    assert compute_kpd(scores) == 8.0


def test_eff_is_undefined_at_or_below_the_floor():
    assert compute_eff(5.0, 2.0) is None
    assert compute_eff(4.9, 1.0) is None
    assert compute_eff(8.0, 4.0) == 2.0
    # max(P, 1) keeps a zero threshold from exploding the ratio
    assert compute_eff(8.0, 0.0) == 8.0


def test_finalize_accepts_a_well_formed_answer(anchors):
    rubric = finalize(
        extract_json(score_answer(i=9.0, v=9.0, u=9.0, d=9.0, nearest_anchors=[])),
        anchors=anchors,
    )
    assert rubric.primary == "SCI"
    assert rubric.secondary == ["TRUST"]
    assert rubric.scores == {"I": 9.0, "V": 9.0, "U": 9.0, "P": 5.0, "S": 6.0, "D": 9.0}
    assert rubric.kpd == 9.0
    assert rubric.eff == 1.8  # 9.0 / max(5.0, 1)
    assert rubric.violations == []
    # the model's own KPD/Eff are ignored, not trusted
    assert rubric.raw["KPD"] == 0.0


def test_finalize_recomputes_a_lying_kpd(anchors):
    answer = extract_json(score_answer(i=6.0, v=6.0, u=6.0, d=6.0))
    answer["KPD"] = 9.99
    answer["Eff"] = 9.99
    rubric = finalize(answer, anchors=anchors)
    assert rubric.kpd == 6.0
    assert rubric.eff == 1.2


def test_missing_axis_is_rejected(anchors):
    answer = extract_json(score_answer())
    del answer["scores"]["D"]
    with pytest.raises(RubricError) as excinfo:
        finalize(answer, anchors=anchors)
    assert "axis 'D'" in str(excinfo.value)


def test_unknown_sphere_is_rejected(anchors):
    answer = extract_json(score_answer(primary="ANTHRO"))
    with pytest.raises(RubricError) as excinfo:
        finalize(answer, anchors=anchors)
    assert "primary must be one of" in str(excinfo.value)


def test_out_of_range_scores_are_clamped_and_recorded(anchors):
    answer = extract_json(score_answer(i=12.5, d=-3.0))
    rubric = finalize(answer, anchors=anchors)
    assert rubric.scores["I"] == 10.0
    assert rubric.scores["D"] == 0.0
    assert any("out of range" in v for v in rubric.violations)


def test_genre_ceiling_caps_i(anchors):
    rubric = finalize(extract_json(score_answer(genre="мемуар", i=9.4)), anchors=anchors)
    assert rubric.scores["I"] == 8.5
    assert any("genre ceiling" in v for v in rubric.violations)


def test_genre_hint_cap_applies_even_if_the_model_disagrees(anchors):
    rubric = finalize(
        extract_json(score_answer(genre="пост", i=9.4)), anchors=anchors, genre_hint="фид-анонс"
    )
    assert rubric.scores["I"] == 7.0


def test_folklore_zeroes_density(anchors):
    rubric = finalize(extract_json(score_answer(d=9.5, flags=["фольклор"])), anchors=anchors)
    assert rubric.scores["D"] == 0.0
    assert any("folklore" in v for v in rubric.violations)


def test_legitimising_text_caps_value(anchors):
    rubric = finalize(extract_json(score_answer(v=9.0, flags=["перформативность"])), anchors=anchors)
    # the flag alone says nothing about legitimation; construction has to say it
    assert rubric.scores["V"] == 9.0

    answer = extract_json(score_answer(v=9.0))
    answer["justification"]["V"] = "текст разрешает существующую иерархию, а не обозначает новую"
    rubric = finalize(answer, anchors=anchors)
    assert rubric.scores["V"] == 4.0
    assert any("rule 10" in v for v in rubric.violations)


def test_honest_denial_of_legitimation_is_not_capped(anchors):
    answer = extract_json(score_answer(v=9.0))
    answer["justification"]["V"] = "текст не разрешает иерархию, а обозначает новую корреляцию"
    rubric = finalize(answer, anchors=anchors)
    assert rubric.scores["V"] == 9.0


def test_secondary_is_capped_at_two_and_unknown_tags_dropped(anchors):
    rubric = finalize(
        extract_json(score_answer(secondary=["DEGR", "HIER", "HYPOC", "BANANA"])),
        anchors=anchors,
    )
    assert rubric.secondary == ["DEGR", "HIER"]
    assert any("unknown secondary" in v for v in rubric.violations)
    assert any("secondary truncated" in v for v in rubric.violations)


def test_drift_from_the_nearest_anchor_downgrades_confidence(anchors):
    # A9 is a weak text (I 3.5); claiming 9.5 next to it is a drift the self-check catches
    rubric = finalize(
        extract_json(score_answer(i=9.5, v=9.5, u=9.5, d=9.5, nearest_anchors=["A9"])),
        anchors=anchors,
    )
    assert rubric.confidence == "low"
    assert any("drift" in v for v in rubric.violations)


def test_close_anchor_keeps_confidence(anchors):
    # A3 «Пуговки»: I 8.6 V 8.8 U 8.7 P 4.5 S 4.5 D 9.0
    rubric = finalize(
        extract_json(
            score_answer(i=8.6, v=8.8, u=8.7, p=4.5, s=4.5, d=9.0, nearest_anchors=["A3"])
        ),
        anchors=anchors,
    )
    assert rubric.confidence == "medium"
    assert not any("drift" in v for v in rubric.violations)
    assert ANCHOR_DRIFT_LIMIT == 0.5


def test_flat_axes_at_the_top_level_are_accepted(anchors):
    """Some models drop the nested ``scores`` object."""
    answer = extract_json(score_answer())
    for axis in ("I", "V", "U", "P", "S", "D"):
        answer[axis] = answer["scores"][axis]
    del answer["scores"]
    rubric = finalize(answer, anchors=anchors)
    assert rubric.scores["I"] == 8.0


# ── JSON extraction ───────────────────────────────────────────────────────────

def test_extract_json_survives_the_full_answer_format():
    payload = extract_json(score_answer(primary="CULT"))
    assert payload["primary"] == "CULT"


def test_extract_json_from_bare_object():
    assert extract_json('{"primary": "SCI"}')["primary"] == "SCI"


def test_extract_json_from_prose_without_fences():
    text = 'Разбор такой-то.\n{"primary": "PERS", "scores": {"I": 1}} и всё.'
    assert extract_json(text)["primary"] == "PERS"


def test_extract_json_reports_garbage_clearly():
    with pytest.raises(RubricError) as excinfo:
        extract_json("совсем не JSON")
    assert "no JSON object" in str(excinfo.value)
    with pytest.raises(RubricError):
        extract_json("")


def test_violations_never_block_the_row(anchors):
    """Recoverable problems are recorded, not fatal — only a non-scoring is fatal."""
    rubric = finalize(
        extract_json(score_answer(i=15.0, secondary=["DEGR", "HIER", "HYPOC", "X"], confidence="?" )),
        anchors=anchors,
    )
    assert len(rubric.violations) >= 3
    assert rubric.confidence in {"high", "medium", "low"}
    assert rubric.kpd > 0
