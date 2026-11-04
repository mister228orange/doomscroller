"""The model backend: NanoGPT by default, DeepSeek behind it.

No network here — ``httpx.MockTransport`` answers instead, so the tests pin the wire format
(URL, auth header, temperature, JSON mode), the retry policy, and the error messages that
matter when someone pays per token: wrong key, empty balance, unknown model.
"""

from __future__ import annotations

import json

import httpx
import pytest

from doomscroller.config import (
    DEFAULT_SCORER_MODEL,
    NANOGPT_BASE_URL,
    load_scorer_config,
)
from doomscroller.scoring.llm import ChatClient, LLMError

SCORER_ENV = (
    "SCORER_BASE_URL",
    "SCORER_MODEL",
    "SCORER_API_KEY",
    "NANOGPT_API_KEY",
)


def _clean_env(monkeypatch) -> None:
    for name in SCORER_ENV:
        monkeypatch.delenv(name, raising=False)


# ── configuration ────────────────────────────────────────────────────────────

def test_defaults_are_nanogpt_with_deepseek(monkeypatch):
    _clean_env(monkeypatch)
    config = load_scorer_config()
    assert config.base_url == NANOGPT_BASE_URL == "https://nano-gpt.com/api/v1"
    assert config.model == DEFAULT_SCORER_MODEL == "deepseek-chat"
    assert config.temperature == 0.0


def test_empty_variables_fall_back_to_the_defaults(monkeypatch):
    _clean_env(monkeypatch)
    # compose interpolates missing values as empty strings; that must not break defaults
    monkeypatch.setenv("SCORER_BASE_URL", "")
    monkeypatch.setenv("SCORER_MODEL", "")
    config = load_scorer_config()
    assert config.base_url == NANOGPT_BASE_URL
    assert config.model == DEFAULT_SCORER_MODEL


def test_nanogpt_api_key_is_accepted_as_a_fallback(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("NANOGPT_API_KEY", "sk-nano")
    assert load_scorer_config().api_key == "sk-nano"


def test_scorer_api_key_wins_over_the_provider_name(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("NANOGPT_API_KEY", "sk-nano")
    monkeypatch.setenv("SCORER_API_KEY", "sk-scorer")
    assert load_scorer_config().api_key == "sk-scorer"


def test_any_openai_compatible_endpoint_still_works(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("SCORER_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("SCORER_MODEL", "qwen2.5:32b")
    config = load_scorer_config()
    assert config.base_url.endswith("11434/v1")
    assert config.model == "qwen2.5:32b"


# ── URL shapes ───────────────────────────────────────────────────────────────

def test_endpoints_for_a_v1_base():
    client = ChatClient(base_url="https://nano-gpt.com/api/v1", model="deepseek/deepseek-v3.2")
    assert client.endpoint == "https://nano-gpt.com/api/v1/chat/completions"
    assert client.models_endpoint == "https://nano-gpt.com/api/v1/models"


def test_a_bare_host_base_gets_v1_appended():
    client = ChatClient(base_url="http://127.0.0.1:11434", model="qwen3:4b")
    assert client.endpoint == "http://127.0.0.1:11434/v1/chat/completions"


def test_missing_base_url_is_an_unretryable_configuration_error():
    with pytest.raises(LLMError) as excinfo:
        ChatClient(base_url="", model="deepseek/deepseek-v3.2")
    assert "SCORER_BASE_URL" in str(excinfo.value)
    assert excinfo.value.retryable is False


def test_missing_model_is_an_unretryable_configuration_error():
    with pytest.raises(LLMError) as excinfo:
        ChatClient(base_url=NANOGPT_BASE_URL, model="")
    assert "SCORER_MODEL" in str(excinfo.value)
    assert excinfo.value.retryable is False


# ── the wire format ──────────────────────────────────────────────────────────

def _ok_response(text: str = '{"primary": "SCI"}', model: str = "deepseek/deepseek-v3.2") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": model,
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 700, "completion_tokens": 120},
        },
    )


async def test_a_call_sends_temperature_zero_an_auth_header_and_json_mode():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok_response()

    client = ChatClient(
        base_url=NANOGPT_BASE_URL,
        model="deepseek/deepseek-v3.2",
        api_key="sk-test",
        transport=httpx.MockTransport(handler),
    )
    response = await client.chat(system="sys", user="usr")

    assert response.text == '{"primary": "SCI"}'
    assert response.model == "deepseek/deepseek-v3.2"
    assert (response.prompt_tokens, response.completion_tokens) == (700, 120)
    assert response.latency_ms is not None

    request = seen[0]
    assert str(request.url) == "https://nano-gpt.com/api/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"
    body = json.loads(request.content)
    assert body["temperature"] == 0.0
    assert body["stream"] is False
    assert body["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


async def test_json_mode_is_dropped_when_the_backend_refuses_it():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"message": "unsupported parameter"}})
        return _ok_response()

    client = ChatClient(
        base_url=NANOGPT_BASE_URL,
        model="deepseek/deepseek-v3.2",
        api_key="sk-test",
        transport=httpx.MockTransport(handler),
    )
    response = await client.chat(system="s", user="u")
    assert response.text
    assert len(bodies) == 2
    assert "response_format" in bodies[0] and "response_format" not in bodies[1]
    # and it does not try JSON mode again on the next call
    await client.chat(system="s", user="u")
    assert "response_format" not in bodies[2]


# ── failure modes that cost money or time ────────────────────────────────────

async def test_a_bad_key_is_not_retried_and_says_so():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    client = ChatClient(
        base_url=NANOGPT_BASE_URL,
        model="deepseek/deepseek-v3.2",
        api_key="sk-wrong",
        retries=3,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError) as excinfo:
        await client.chat(system="s", user="u")
    assert calls == 1, "an auth error must not be retried"
    assert "authentication failed" in str(excinfo.value)
    assert "SCORER_API_KEY" in str(excinfo.value)
    assert excinfo.value.status == 401
    assert excinfo.value.retryable is False
    # and it does not pretend to have retried
    assert "attempt(s)" not in str(excinfo.value)


async def test_an_empty_balance_is_reported_as_payment_required():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": {"message": "insufficient balance"}})

    client = ChatClient(
        base_url=NANOGPT_BASE_URL,
        model="deepseek/deepseek-v3.2",
        api_key="sk-test",
        retries=0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError) as excinfo:
        await client.ping()
    assert "balance is empty" in str(excinfo.value)


async def test_an_unknown_model_points_at_the_model_listing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "model not found"}})

    client = ChatClient(
        base_url=NANOGPT_BASE_URL,
        model="deepseek/nope",
        api_key="sk-test",
        retries=0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError) as excinfo:
        await client.chat(system="s", user="u")
    assert "list-models" in str(excinfo.value)


async def test_server_errors_are_retried_then_reported():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="upstream overloaded")

    client = ChatClient(
        base_url=NANOGPT_BASE_URL,
        model="deepseek/deepseek-v3.2",
        api_key="sk-test",
        retries=1,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError) as excinfo:
        await client.chat(system="s", user="u")
    assert calls == 2
    assert excinfo.value.retryable is True


async def test_rate_limiting_is_retryable():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        return _ok_response()

    client = ChatClient(
        base_url=NANOGPT_BASE_URL,
        model="deepseek/deepseek-v3.2",
        api_key="sk-test",
        retries=1,
        transport=httpx.MockTransport(handler),
    )
    response = await client.chat(system="s", user="u")
    assert response.text and calls == 2


async def test_an_empty_answer_names_the_reasoning_field():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "deepseek/deepseek-v3.2:thinking",
                "choices": [{"message": {"role": "assistant", "content": "",
                                         "reasoning_content": "Думаю очень долго..."}}],
            },
        )

    client = ChatClient(
        base_url=NANOGPT_BASE_URL,
        model="deepseek/deepseek-v3.2:thinking",
        api_key="sk-test",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError) as excinfo:
        await client.chat(system="s", user="u")
    assert "empty message" in str(excinfo.value)
    assert "only reasoning" in str(excinfo.value)


# ── diagnostics ─────────────────────────────────────────────────────────────

async def test_ping_is_a_real_completion():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response("ok")

    client = ChatClient(
        base_url=NANOGPT_BASE_URL,
        model="deepseek/deepseek-v3.2",
        api_key="sk-test",
        transport=httpx.MockTransport(handler),
    )
    response = await client.ping()
    assert response.text == "ok"
    assert response.latency_ms is not None


async def test_list_models_filters_and_sorts():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/models")
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "deepseek/deepseek-v3.2"},
                    {"id": "deepseek/deepseek-v4.1-flash"},
                    {"id": "openai/gpt-x"},
                ],
            },
        )

    client = ChatClient(
        base_url=NANOGPT_BASE_URL,
        model="deepseek/deepseek-v3.2",
        api_key="sk-test",
        transport=httpx.MockTransport(handler),
    )
    assert await client.list_models() == [
        "deepseek/deepseek-v3.2",
        "deepseek/deepseek-v4.1-flash",
        "openai/gpt-x",
    ]
    assert await client.list_models("v4") == ["deepseek/deepseek-v4.1-flash"]
