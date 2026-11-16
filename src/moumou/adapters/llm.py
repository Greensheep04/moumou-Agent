"""LLM clients.

DeepSeek speaks the OpenAI-compatible wire format, but we talk to it with plain
``httpx`` rather than a vendor SDK: the dependency tree stays clean (no
``openai`` package anywhere), and token accounting plus budget feedback are
things the orchestrator needs to own anyway.
"""

from __future__ import annotations

import asyncio
import json
import os
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

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        self.calls.append(messages)
        if callable(self._script):
            return self._script(messages, tools)
        if not self._script:
            return LLMResponse(content="(fake llm: script exhausted)")
        return self._script.pop(0)


class DeepSeekClient:
    """Real client. Set DEEPSEEK_API_KEY (or pass api_key)."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_s: float = 180.0,
        max_retries: int = 3,
    ):
        from moumou.config import DEFAULT_BASE_URL, DEFAULT_MODEL, load_dotenv

        load_dotenv()
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self.base_url = (
            base_url or os.environ.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self.model = model or os.environ.get("DEEPSEEK_MODEL") or DEFAULT_MODEL
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

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY is not set. cp .env.example .env and put your "
                "own key there (.env is gitignored)."
            )

        payload: dict = {"model": self.model, "messages": messages, "temperature": 0.2}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = await self._http().post(
                    f"{self.base_url}/chat/completions", json=payload, headers=headers
                )
            except httpx.HTTPError as exc:              # network blip: back off
                last_err = exc
                await asyncio.sleep(2**attempt)
                continue

            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries - 1:
                await asyncio.sleep(2**attempt)
                last_err = RuntimeError(f"HTTP {resp.status_code}")
                continue

            if resp.status_code != 200:
                raise RuntimeError(f"DeepSeek HTTP {resp.status_code}: {resp.text[:500]}")

            return self._parse(resp.json())

        raise RuntimeError(f"DeepSeek failed after {self.max_retries} attempts: {last_err}")

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

        usage = data.get("usage") or {}
        return LLMResponse(
            content=msg.get("content") or "",
            tool_calls=calls,
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            cache_hit_tokens=int(usage.get("prompt_cache_hit_tokens") or 0),
            model=data.get("model", self.model),
        )
