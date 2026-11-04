"""Scoring: the six-axis rubric, the channel trajectory, and the agent that runs them.

Layout:

* :mod:`.prompts`  — loads ``scoring/*.md`` and assembles the prompt
* :mod:`.rubric`   — parses and validates the model's answer, applies the self-check
* :mod:`.llm`      — the OpenAI-compatible chat client
* :mod:`.state`    — channel trajectory (moving means, deviations, shifts, memory)
* :mod:`.agent`    — the scheduler-driven run: unread post -> score -> state -> DB

Exports are resolved lazily (PEP 562) on purpose. The MCP server only *reads* scores, and
it may run on a machine that is not the scorer — importing ``doomscroller.scoring.state``
must not drag in the agent, the chat client and ``httpx`` with it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "Anchor",
    "ChannelReport",
    "ChatClient",
    "LLMError",
    "Rubric",
    "RubricError",
    "RubricModules",
    "ScoreReport",
    "compute_eff",
    "compute_kpd",
    "finalize",
    "load_modules",
    "parse_anchors",
    "render_channel_state",
    "render_prompt",
    "run_agent",
    "run_loop",
]

_LAZY: dict[str, str] = {
    "Anchor": ".rubric",
    "Rubric": ".rubric",
    "RubricError": ".rubric",
    "compute_eff": ".rubric",
    "compute_kpd": ".rubric",
    "finalize": ".rubric",
    "parse_anchors": ".rubric",
    "RubricModules": ".prompts",
    "load_modules": ".prompts",
    "render_channel_state": ".prompts",
    "render_prompt": ".prompts",
    "ChatClient": ".llm",
    "LLMError": ".llm",
    "ChannelReport": ".agent",
    "ScoreReport": ".agent",
    "run_agent": ".agent",
    "run_loop": ".agent",
}

if TYPE_CHECKING:  # pragma: no cover - names for editors/type checkers only
    from .agent import ChannelReport, ScoreReport, run_agent, run_loop
    from .llm import ChatClient, LLMError
    from .prompts import RubricModules, load_modules, render_channel_state, render_prompt
    from .rubric import (
        Anchor,
        Rubric,
        RubricError,
        compute_eff,
        compute_kpd,
        finalize,
        parse_anchors,
    )


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value  # cache: subsequent lookups skip this hook
    return value


def __dir__() -> list[str]:  # pragma: no cover - introspection nicety
    return sorted(__all__)
