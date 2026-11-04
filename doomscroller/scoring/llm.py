"""The one place that talks to a language model.

Deliberately provider-agnostic, but with a default that matches how this is actually
deployed: **NanoGPT** (``https://nano-gpt.com/api/v1``) serving a DeepSeek model. Every
backend worth using here speaks the OpenAI ``/chat/completions`` shape — NanoGPT, DeepSeek
directly, a local Ollama (``http://host:11434/v1``) — so there is no provider switch to get
wrong: a base URL, a key and a model id.

Two properties matter for a scorer that writes to a database:

* **temperature 0** — the methodology asks for a reproducible score, not a sample.
* **the answer is data, not code** — the raw text goes into ``raw`` on the score row, and
  parsing happens in :mod:`doomscroller.scoring.rubric`, where a malformed answer is a
  validation error rather than a crash mid-run.

Paying for an endpoint changes the failure modes, so they are made explicit here: auth,
empty balance, unknown model and context overflow each produce their own message, and a
401 is not retried three times.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

#: What a wrong HTTP status usually means when the endpoint is NanoGPT or a clone of it.
_STATUS_HINTS: dict[int, str] = {
    400: "the backend rejected the request (a malformed prompt, or a model that refuses this shape)",
    401: "authentication failed — set SCORER_API_KEY (or NANOGPT_API_KEY)",
    402: "payment required — the provider balance is empty",
    403: "forbidden — the key may not have access to this model",
    404: "not found — check SCORER_MODEL / SCORER_BASE_URL (`doomscroller score --list-models`)",
    413: "prompt too large for the model's context — lower SCORER_MAX_CHARS",
    422: "the backend could not parse the request body",
    429: "rate limited or out of balance — retry later",
}


class LLMError(RuntimeError):
    """The backend could not answer.

    ``retryable`` separates "try again in a moment" from "this will never work with the
    current configuration" — retrying a 401 three times only hides the real message.
    """

    def __init__(self, message: str, *, retryable: bool = True, status: Optional[int] = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass
class LLMResponse:
    text: str
    model: str
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    latency_ms: Optional[int] = None
    raw: dict[str, Any] = field(default_factory=dict)


class ChatClient:
    """Minimal async OpenAI-compatible client with bounded retries."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        temperature: float = 0.0,
        timeout: float = 600.0,
        retries: int = 2,
        json_mode: str = "auto",
        max_tokens: Optional[int] = None,
        extra_headers: Optional[dict[str, str]] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        if not base_url:
            raise LLMError(
                "SCORER_BASE_URL is not set. The default is NanoGPT "
                "(https://nano-gpt.com/api/v1); a local Ollama is "
                "http://127.0.0.1:11434/v1 (compose: http://host.docker.internal:11434/v1).",
                retryable=False,
            )
        if not model:
            raise LLMError(
                "SCORER_MODEL is not set. With NanoGPT try `deepseek-chat` (original DeepSeek V3) "
                "or `deepseek/deepseek-v3.2` (see `doomscroller score --list-models`).",
                retryable=False,
            )

        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.retries = max(0, retries)
        self.json_mode = json_mode.lower()
        self.max_tokens = max_tokens
        self._extra_headers = extra_headers or {}
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None
        self._json_mode_works = self.json_mode != "off"

    # ── plumbing ─────────────────────────────────────────────────────────────

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/chat/completions"
        return f"{self.base_url}/v1/chat/completions"

    @property
    def models_endpoint(self) -> str:
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/models"
        return f"{self.base_url}/v1/models"

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self._extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def __aenter__(self) -> "ChatClient":
        self._client = httpx.AsyncClient(timeout=self.timeout, transport=self._transport)
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout, transport=self._transport)
        return self._client

    @staticmethod
    def _error_from(response: httpx.Response) -> LLMError:
        status = response.status_code
        hint = _STATUS_HINTS.get(status, "unexpected backend error")
        retryable = status in {408, 409, 425, 429} or status >= 500
        return LLMError(
            f"HTTP {status} — {hint}: {response.text[:400]}",
            retryable=retryable,
            status=status,
        )

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        client = self._ensure_client()
        response = await client.post(self.endpoint, headers=self._headers, json=payload)
        if response.status_code == 400 and "response_format" in payload:
            # Not every OpenAI-compatible server implements JSON mode (some proxies strip
            # it). Drop it and remember, instead of failing every post on this backend.
            logger.warning("%s rejected response_format; disabling JSON mode", self.endpoint)
            self._json_mode_works = False
            payload = {k: v for k, v in payload.items() if k != "response_format"}
            response = await client.post(self.endpoint, headers=self._headers, json=payload)
        if response.status_code >= 400:
            raise self._error_from(response)
        try:
            return response.json()
        except json.JSONDecodeError as exc:  # pragma: no cover - backend bug
            raise LLMError(f"backend returned non-JSON: {response.text[:300]}") from exc

    async def _get_models(self) -> list[str]:
        client = self._ensure_client()
        response = await client.get(self.models_endpoint, headers=self._headers)
        if response.status_code >= 400:
            raise self._error_from(response)
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise LLMError("model list is not JSON") from exc
        rows = data.get("data") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise LLMError(f"unexpected model list shape: {str(data)[:200]}")
        ids: list[str] = []
        for row in rows:
            if isinstance(row, dict) and row.get("id"):
                ids.append(str(row["id"]))
            elif isinstance(row, str):
                ids.append(row)
        return ids

    async def chat(self, *, system: str, user: str) -> LLMResponse:
        """One completion. Retries transient failures with a short backoff."""
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        }
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens
        if self.json_mode == "on" or (self.json_mode == "auto" and self._json_mode_works):
            payload["response_format"] = {"type": "json_object"}

        last_error: Optional[Exception] = None
        started = time.monotonic()
        for attempt in range(self.retries + 1):
            try:
                data = await self._post(payload)
                return self._parse(data, int((time.monotonic() - started) * 1000))
            except LLMError as exc:
                last_error = exc
                if not exc.retryable:
                    # Configuration, auth or balance: retrying cannot help, and wrapping it
                    # as "failed after N attempt(s)" would misreport what happened.
                    raise
                if attempt >= self.retries:
                    break
                delay = 0.5 * (2**attempt)
                logger.warning(
                    "LLM call failed (%s); retry %d/%d in %.1fs",
                    exc, attempt + 1, self.retries, delay,
                )
                await asyncio.sleep(delay)
            except httpx.HTTPError as exc:  # timeouts, DNS, connection resets
                last_error = exc
                if attempt >= self.retries:
                    break
                delay = 0.5 * (2**attempt)
                logger.warning(
                    "LLM transport error (%s); retry %d/%d in %.1fs",
                    exc, attempt + 1, self.retries, delay,
                )
                await asyncio.sleep(delay)
        if self.retries == 0 and isinstance(last_error, LLMError):
            # No retries configured (the doctor does this): report the real reason, not
            # "failed after 1 attempt".
            raise last_error
        raise LLMError(
            f"LLM call failed after {self.retries + 1} attempt(s): {last_error}",
            retryable=getattr(last_error, "retryable", True),
            status=getattr(last_error, "status", None),
        )

    def _parse(self, data: dict[str, Any], latency_ms: int) -> LLMResponse:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError(f"backend answer has no choices: {json.dumps(data)[:300]}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # some servers return content parts
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        usage = data.get("usage") or {}
        if not isinstance(content, str) or not content.strip():
            # A reasoning model can burn its budget on thinking and return nothing usable.
            hints = ", ".join(str(message.get(key) or "") for key in ("reasoning", "reasoning_content"))
            raise LLMError(
                "backend returned an empty message"
                + (f" (only reasoning was produced: {hints[:200]})" if hints.strip(", ") else "")
            )
        return LLMResponse(
            text=content,
            model=str(data.get("model") or self.model),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            latency_ms=latency_ms,
            raw=data,
        )

    # ── diagnostics (used by `score --check` / `--list-models`) ───────────────

    async def ping(self) -> LLMResponse:
        """One tiny completion: proves the URL, the key and the model id all work."""
        return await self.chat(
            system="You are a health probe. Answer with a single word.",
            user='Reply with the word "ok".',
        )

    async def list_models(self, contains: Optional[str] = None) -> list[str]:
        ids = await self._get_models()
        if contains:
            needle = contains.lower()
            ids = [model_id for model_id in ids if needle in model_id.lower()]
        return sorted(ids)
