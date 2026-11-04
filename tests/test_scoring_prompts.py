"""The rubric files and the prompt assembled from them."""

from __future__ import annotations

import pytest

from doomscroller.scoring.prompts import (
    SUMMARY_MODULE,
    SYSTEM_MODULES,
    TASK_MODULE,
    RubricError,
    load_modules,
    render_channel_state,
    render_prompt,
)


def test_every_module_loads_and_is_hashed():
    modules = load_modules()
    assert len(modules.version) == 12
    for name in (*SYSTEM_MODULES, TASK_MODULE, SUMMARY_MODULE):
        assert modules.texts[name].strip(), f"{name} is empty"


def test_system_prompt_carries_the_rubric_but_not_the_task():
    modules = load_modules()
    system = modules.system_text()
    # the axes, the rules, the anchors and the self-check are all in the system prompt
    assert "# ОСИ" in system
    assert "# ПРАВИЛА" in system
    assert "A1" in system and "A10" in system
    assert "# САМОПРОВЕРКА" in system
    assert "# КОНТЕКСТ КАНАЛА" in system
    # the procedure and the answer format belong to the user turn
    assert "# ПРОЦЕДУРА" not in system
    assert "# ФОРМАТ ОТВЕТА" not in system


def test_prompt_has_no_unfilled_placeholders():
    modules = load_modules()
    prompt = render_prompt(
        modules,
        text="Текст поста про метод.",
        genre=None,
        channel_state=None,
    )
    for rendered in (prompt.system, prompt.user):
        assert "{{" not in rendered and "}}" not in rendered


def test_prompt_points_at_the_data_block():
    modules = load_modules()
    prompt = render_prompt(
        modules,
        text="Тело поста.",
        context="контекст канала",
        genre="трактат",
        channel_state="TREND: I 8.0",
    )
    assert "TEXT:" in prompt.system and "в блоке ДАННЫЕ" in prompt.system
    assert "трактат" in prompt.system
    # the post text appears exactly once — in the user turn
    assert prompt.user.count("Тело поста.") == 1
    assert "Тело поста." not in prompt.system
    assert "TREND: I 8.0" in prompt.user
    assert prompt.user.index("# ДАННЫЕ") < prompt.user.index("Тело поста.")
    assert "Верни только JSON" in prompt.user


def test_prompt_marks_absent_context_explicitly():
    modules = load_modules()
    prompt = render_prompt(modules, text="Текст.")
    assert "— не передан" in prompt.system
    assert "канал оценивается впервые" in prompt.user
    assert "— не передан (определи сам)" in prompt.user


def test_long_text_is_capped_with_a_visible_marker():
    modules = load_modules()
    prompt = render_prompt(modules, text="я" * 500, max_chars=100)
    assert len(prompt.user) < 4000
    assert "текст обрезан по лимиту" in prompt.user


def test_channel_state_block_renders_the_trajectory():
    snapshot = {
        "profile_description": "Авторский канал: метод и язык",
        "summary": "темы: метод; сдвигов нет",
        "means": {"I": 8.6, "V": 8.4, "U": 9.0, "P": 6.2, "S": 6.0, "D": 7.8},
        "kpd_mean": 8.45,
        "eff_mean": 1.36,
        "window": 5,
        "total_scored": 12,
        "local_anchors": [
            {
                "id": "p_45",
                "title": "Пуговки",
                "primary": "SCI",
                "scores": {"I": 8.6, "V": 8.8, "U": 8.7, "P": 4.5, "S": 4.5, "D": 9.0},
                "kpd": 8.78,
            }
        ],
        "tag_distribution": {"SCI": 5, "TECH": 3},
        "events": [{"date": "2026-11-01", "kind": "peak", "note": "пик канала"}],
    }
    block = render_channel_state(snapshot)
    assert "PROFILE: Авторский канал" in block
    assert "TREND: I 8.6 | V 8.4" in block
    assert "КПД 8.45" in block and "Эфф 1.36" in block
    assert "p_45 «Пуговки» [SCI]" in block
    assert "TAG_DIST: SCI 5, TECH 3" in block
    assert "SUMMARY:" in block
    assert "EVENTS:" in block and "peak" in block


def test_channel_state_block_for_a_brand_new_channel():
    block = render_channel_state(
        {"total_scored": 0, "means": {}, "local_anchors": [], "tag_distribution": {}}
    )
    assert "первый пост канала" in block
    assert "LOCAL_ANCHORS: — (нет)" in block


def test_missing_module_is_a_clear_error(tmp_path):
    with pytest.raises(RubricError) as excinfo:
        load_modules(tmp_path)
    assert "missing rubric module" in str(excinfo.value)
