"""LLM clients.

DeepSeek speaks the OpenAI-compatible wire format, but we talk to it with plain
``httpx`` rather than a vendor SDK: the dependency tree stays clean (no
``openai`` package anywhere), and token accounting plus budget feedback are
things the orchestrator needs to own anyway.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str
    name: str
    args: dict = Field(default_factory=dict)


class LLMResponse(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cache_hit_tokens: int = 0      # reported for the cost analysis, not for budgets
    model: str = ""
    # None means an old/scripted caller did not report whether usage was measured.
    # Keep the legacy integer fields for budget compatibility, but never infer a
    # measured zero from their defaults.
    usage_present: bool | None = None
    usage_raw: dict[str, int] = Field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out


@runtime_checkable
class LLMClient(Protocol):
    async def chat(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> LLMResponse: ...


Responder = Callable[[list[dict], list[dict] | None], LLMResponse]


class FakeLLM:
    """Deterministic client for tests and the walking skeleton.

    Accepts either a queue of canned responses or a function of the messages.
    Use the function form for anything concurrent — a queue is order-sensitive.
    """

    def __init__(self, script: list[LLMResponse] | Responder):
        self._script = script
        self.calls: list[list[dict]] = []

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        diag: list[dict] | None = None,
    ) -> LLMResponse:
        """One completion call.

        ``diag`` (optional) collects one record per HTTP attempt so a timeout can
        be attributed: which attempt, what status, how long it took. The caller
        owns the list; nothing is shared between calls.
        """
        self.calls.append(messages)
        if callable(self._script):
            return self._script(messages, tools)
        if not self._script:
            return LLMResponse(content="(fake llm: script exhausted)")
        return self._script.pop(0)


class DeepSeekClient:
    """Buffered client for either configured model endpoint."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_s: float | None = None,
        max_retries: int = 2,
    ):
        from moumou.config import LLMSettings

        if api_key is None or base_url is None or model is None or timeout_s is None:
            settings = (LLMSettings.from_env() if api_key is None
                        else LLMSettings.public_from_env())
            api_key = settings.api_key if api_key is None else api_key
            base_url = base_url or settings.base_url
            model = model or settings.model
            timeout_s = settings.timeout_s if timeout_s is None else timeout_s
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout_s)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        diag: list[dict] | None = None,
    ) -> LLMResponse:
        """One completion call.

        ``diag`` (optional) collects one record per HTTP attempt so a timeout can
        be attributed: which attempt, what status, how long it took. The caller
        owns the list; nothing is shared between calls.
        """
        if not self.api_key:
            raise RuntimeError(
                "No model API key configured. See .env.example for the selected profile."
            )

        payload: dict = {"model": self.model, "messages": messages, "temperature": 0.2}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with asyncio.timeout(self.timeout_s):
            return await self._request(payload, headers, diag)

    async def _request(self, payload, headers, diag) -> LLMResponse:
        for attempt in range(self.max_retries):
            started = time.monotonic()
            try:
                resp = await self._http().post(
                    f"{self.base_url}/chat/completions", json=payload, headers=headers
                )
            except (httpx.TransportError, asyncio.CancelledError) as exc:
                retrying = isinstance(exc, httpx.TransportError) and attempt + 1 < self.max_retries
                if diag is not None:
                    diag.append({
                        "attempt": attempt + 1, "status": "transport_error",
                        "error": type(exc).__name__,
                        "elapsed_s": round(time.monotonic() - started, 3),
                        "retrying": retrying,
                    })
                if not retrying:
                    raise
                await asyncio.sleep(2**attempt)
                continue

            elapsed = round(time.monotonic() - started, 3)
            if resp.status_code in (429, 502, 503, 504) and attempt < self.max_retries - 1:
                if diag is not None:
                    diag.append({
                        "attempt": attempt + 1, "status": resp.status_code,
                        "elapsed_s": elapsed, "retrying": True,
                    })
                await asyncio.sleep(2**attempt)
                continue

            if diag is not None:
                diag.append({
                    "attempt": attempt + 1, "status": resp.status_code,
                    "elapsed_s": elapsed, "retrying": False,
                })
            if resp.status_code != 200:
                raise RuntimeError(f"DeepSeek HTTP {resp.status_code}: {resp.text[:500]}")

            return self._parse(resp.json())

        raise ValueError("max_retries must allow at least one HTTP attempt")

    def _parse(self, data: dict) -> LLMResponse:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}

        calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except json.JSONDecodeError:
                args = {"__unparsed__": raw}
            calls.append(ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), args=args))

        usage = data.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        usage_fields = (
            "prompt_tokens", "completion_tokens", "total_tokens",
            "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
        )
        observed_usage = {
            key: usage[key] for key in usage_fields
            if type(usage.get(key)) is int and usage[key] >= 0
        }
        return LLMResponse(
            content=msg.get("content") or "",
            tool_calls=calls,
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            cache_hit_tokens=int(usage.get("prompt_cache_hit_tokens") or 0),
            model=data.get("model", self.model),
            usage_present=all(key in observed_usage for key in (
                "prompt_tokens", "completion_tokens"
            )),
            usage_raw=observed_usage,
        )
