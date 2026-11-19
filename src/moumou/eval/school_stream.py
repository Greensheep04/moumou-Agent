"""Optional school SSE client; it is not registered by the default CLI.

Only complete, bounded, single-choice SSE responses become LLMResponse objects.
Failed attempts never expose partial content or executable tool arguments.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import math
import re
import time
from collections.abc import Callable
from uuid import uuid4

import httpx

from moumou.adapters.llm import DeepSeekClient, LLMResponse

USAGE_FIELDS = (
    "prompt_tokens", "completion_tokens", "total_tokens",
    "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
)


class SchoolStreamError(RuntimeError):
    """An HTTP/protocol/bound failure, without provider body or credential text."""


class SchoolStreamTimeout(TimeoutError):
    """The bounded overall attempt deadline expired."""


class SchoolStreamHTTPError(SchoolStreamError):
    """Only raised on HTTP headers, before any response body is consumed."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"School stream HTTP {status_code}")


def _positive(value: object, name: str, *, integer: bool = False) -> None:
    if (type(value) not in ((int,) if integer else (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError(f"{name} must be a positive {'integer' if integer else 'number'}")


def _identifier(value: object) -> str | None:
    if (isinstance(value, str) and len(value) <= 120
            and re.fullmatch(r"[A-Za-z0-9_.:/+ -]+", value)
            and not re.search(r"(?:sk-|tvly-|ghp_|github_pat_|Bearer\s)", value, re.I)):
        return value
    return None


def _object_pairs(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def _json(raw: str) -> object:
    def no_constant(_value: str) -> None:
        raise ValueError("Non-finite JSON constant")

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("Non-finite JSON number")
        return parsed

    try:
        value = json.loads(raw, object_pairs_hook=_object_pairs, parse_constant=no_constant,
                           parse_float=finite_float)
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return value
    except (ValueError, RecursionError) as error:
        raise SchoolStreamError("Invalid JSON in school stream") from error


def _offered_tool_names(tools: object) -> frozenset[str]:
    """Bind a required response to this request's unambiguous function names."""
    if not isinstance(tools, list) or not tools:
        raise SchoolStreamError("Required tool choice needs nonempty function schemas")
    names = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if (not isinstance(tool, dict) or tool.get("type") != "function"
                or not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}", name)):
            raise SchoolStreamError("Required tool choice has invalid function schemas")
        names.append(name)
    if len(names) != len(set(names)):
        raise SchoolStreamError("Required tool choice has ambiguous function names")
    return frozenset(names)


class _Assembly:
    def __init__(self, client: SchoolStreamClient, clock: Callable[[], float]) -> None:
        self.client, self.clock = client, clock
        self.content = ""
        self.content_chars_received = 0
        self.content_bound_exceeded = False
        self.reasoning_chars = 0
        self.tools: dict[int, dict[str, str]] = {}
        self.finish: str | None = None
        self.model = ""
        self.usage: dict[str, int] = {}
        self.first_content_s: float | None = None
        self.first_tool_s: float | None = None
        self.required_tool_choice_satisfied: bool | None = None

    def consume(self, value: object) -> None:
        if not isinstance(value, dict) or "error" in value:
            raise SchoolStreamError("School provider error or invalid SSE object")
        model = value.get("model")
        if model is not None:
            if not isinstance(model, str) or len(model) > 256:
                raise SchoolStreamError("Invalid returned model identity")
            if self.model and model != self.model:
                raise SchoolStreamError("Returned model changed within one attempt")
            self.model = model
        usage = value.get("usage")
        if isinstance(usage, dict):
            self.usage = {key: usage[key] for key in USAGE_FIELDS
                          if type(usage.get(key)) is int and usage[key] >= 0}
        choices = value.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise SchoolStreamError("School stream requires exactly one choice")
        if not choices:  # A usage-only frame is valid, not an empty completion.
            return
        choice = choices[0]
        index = choice.get("index", 0) if isinstance(choice, dict) else None
        if not isinstance(choice, dict) or type(index) is not int or index != 0:
            raise SchoolStreamError("Invalid school choice index")
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict) or delta.get("role") not in (None, "assistant"):
            raise SchoolStreamError("Invalid assistant delta")
        content, tools = delta.get("content"), delta.get("tool_calls")
        if self.finish is not None and (content or tools):
            raise SchoolStreamError("Assistant data appeared after terminal finish")
        if content is not None:
            if not isinstance(content, str):
                raise SchoolStreamError("Non-text assistant content")
            if content and self.first_content_s is None:
                self.first_content_s = self.clock()
            self.content_chars_received += len(content)
            if self.content_chars_received > self.client.max_content_chars:
                self.content_bound_exceeded = True
                raise SchoolStreamError("School content bound exceeded")
            self.content += content
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning, str):
            self.reasoning_chars += len(reasoning)
        if tools is not None:
            self._tools(tools)
        finish = choice.get("finish_reason")
        if finish is not None:
            if self.finish is not None:
                raise SchoolStreamError("Repeated terminal finish")
            self.finish = _identifier(finish)
            if not isinstance(finish, str) or finish not in {"stop", "tool_calls"}:
                raise SchoolStreamError("School response did not finish completely")

    def _tools(self, calls: object) -> None:
        if not isinstance(calls, list) or len(calls) > self.client.max_tools:
            raise SchoolStreamError("Invalid or oversized tool delta list")
        for call in calls:
            if not isinstance(call, dict):
                raise SchoolStreamError("Invalid tool delta")
            index = call.get("index")
            if type(index) is not int or not 0 <= index < self.client.max_tools:
                raise SchoolStreamError("Invalid tool index")
            if call.get("type") not in (None, "function"):
                raise SchoolStreamError("Unsupported tool type")
            part = self.tools.setdefault(index, {"id": "", "name": "", "arguments": ""})
            identity = call.get("id")
            if identity is not None:
                if not isinstance(identity, str) or len(identity) > 256:
                    raise SchoolStreamError("Invalid tool identity")
                if part["id"] and identity != part["id"]:
                    raise SchoolStreamError("Conflicting tool identity")
                part["id"] = identity
            function = call.get("function") or {}
            if not isinstance(function, dict):
                raise SchoolStreamError("Invalid function delta")
            for key in ("name", "arguments"):
                value = function.get(key)
                if value is not None:
                    if not isinstance(value, str):
                        raise SchoolStreamError("Non-text function delta")
                    part[key] += value
            if len(part["name"]) > 128 or len(part["arguments"]) > self.client.max_argument_chars:
                raise SchoolStreamError("School tool assembly bound exceeded")
            if self.first_tool_s is None:
                self.first_tool_s = self.clock()

    def response(self, required_names: frozenset[str] | None = None) -> LLMResponse:
        if self.finish is None:
            raise SchoolStreamError("School stream lacks terminal finish")
        calls = []
        identities: set[str] = set()
        for index, part in sorted(self.tools.items()):
            if (index != len(calls) or not part["id"]
                    or part["id"] in identities
                    or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}", part["name"])):
                raise SchoolStreamError("Incomplete final tool identity/index/name")
            identities.add(part["id"])
            args = _json(part["arguments"])
            if not isinstance(args, dict):
                raise SchoolStreamError("Final function arguments must be a JSON object")
            calls.append({"id": part["id"], "type": "function", "function": {
                "name": part["name"], "arguments": json.dumps(args, ensure_ascii=False),
            }})
        if ((self.finish == "tool_calls") != bool(calls)
                or not (self.content or calls)):
            raise SchoolStreamError("Finish reason does not match completed assistant output")
        if required_names is not None:
            self.required_tool_choice_satisfied = bool(calls) and all(
                call["function"]["name"] in required_names for call in calls
            )
            if not self.required_tool_choice_satisfied:
                raise SchoolStreamError("Required tool choice was not satisfied")
        # Only normalized, validated complete output reaches the unchanged parent parser.
        return self.client._parse({"model": self.model or self.client.model, "usage": self.usage,
                                   "choices": [{"message": {
                                       "content": self.content, "tool_calls": calls,
                                   }}]})


class _SSE:
    def __init__(self, assembly: _Assembly, client: SchoolStreamClient) -> None:
        self.assembly, self.client = assembly, client
        self.decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
        self.pending = ""
        self.data: list[str] = []
        self.event_bytes = 0
        self.events = 0
        self.done = False
        self.first_event_s: float | None = None

    def feed(self, chunk: bytes, *, final: bool = False) -> None:
        try:
            self.pending += self.decoder.decode(chunk, final=final)
        except UnicodeDecodeError as error:
            raise SchoolStreamError("Invalid UTF-8 in school stream") from error
        while self.pending:
            ending = re.search(r"[\r\n]", self.pending)
            if ending is None:
                break
            index = ending.start()
            if self.pending[index:] == "\r" and not final:
                break
            width = 2 if self.pending[index:index + 2] == "\r\n" else 1
            line, self.pending = self.pending[:index], self.pending[index + width:]
            self._line(line)
        if len(self.pending.encode("utf-8")) > self.client.max_event_bytes:
            raise SchoolStreamError("School SSE line bound exceeded")
        if final and self.pending:
            self._line(self.pending)
            self.pending = ""
        if final and self.data:
            raise SchoolStreamError("School stream ended within an SSE event")

    def _line(self, line: str) -> None:
        if len(line.encode("utf-8")) > self.client.max_event_bytes:
            raise SchoolStreamError("School SSE line bound exceeded")
        if line == "" and self.data:
            data = "\n".join(self.data)
            self.data, self.event_bytes = [], 0
            self.events += 1
            if self.events > self.client.max_events or self.done:
                raise SchoolStreamError("School SSE event bound or trailing event exceeded")
            if self.first_event_s is None:
                self.first_event_s = self.assembly.clock()
            if data == "[DONE]":
                self.done = True
            else:
                self.assembly.consume(_json(data))
        elif line.startswith("data:"):
            value = line[5:]
            value = value[1:] if value.startswith(" ") else value
            self.event_bytes += len(value.encode("utf-8")) + 1
            if self.event_bytes > self.client.max_event_bytes:
                raise SchoolStreamError("School SSE event byte bound exceeded")
            self.data.append(value)


class SchoolStreamClient(DeepSeekClient):
    """Explicit opt-in SSE transport, compatible with the existing LLM protocol.

The default remains auto tool choice; required-when-tools is an explicit policy
for requests carrying nonempty tool schemas. No automatic fallback or stream_options
exists. A second attempt needs both max_attempts=2 and explicit
retry_transport=True; only httpx.TransportError is eligible, never HTTP/protocol
errors, output length, cancellation or the overall deadline. Separate explicit
transient_http_retries allows only pre-body 502/503/504 retries with 5s/15s
backoff inside one shared call deadline. These two retry policies cannot combine.
Reasoning control is omitted unless reasoning_effort="none" is explicitly selected.
"""

    def __init__(
        self, api_key: str | None = None, base_url: str | None = None,
        model: str | None = None, timeout_s: float = 180.0, *, read_timeout_s: float = 60.0,
        max_attempts: int = 1, retry_transport: bool = False, completion_cap: int | None = None,
        reasoning_effort: str | None = None,
        tool_choice_policy: str = "auto",
        transient_http_retries: int = 0,
        max_stream_bytes: int = 8 * 1024 * 1024, max_event_bytes: int = 256 * 1024,
        max_events: int = 50000, max_content_chars: int = 2 * 1024 * 1024,
        max_argument_chars: int = 256 * 1024, max_tools: int = 32,
        on_attempt: Callable[[dict], None] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        for name, value in (("timeout_s", timeout_s), ("read_timeout_s", read_timeout_s)):
            _positive(value, name)
        for name, value in (("max_stream_bytes", max_stream_bytes),
                            ("max_event_bytes", max_event_bytes), ("max_events", max_events),
                            ("max_content_chars", max_content_chars),
                            ("max_argument_chars", max_argument_chars), ("max_tools", max_tools)):
            _positive(value, name, integer=True)
            setattr(self, name, value)
        if (type(max_attempts) is not int or max_attempts not in (1, 2)
                or type(retry_transport) is not bool
                or (max_attempts == 2 and not retry_transport)):
            raise ValueError("A second attempt requires explicit transport retry policy")
        if completion_cap is not None:
            _positive(completion_cap, "completion_cap", integer=True)
        if type(transient_http_retries) is not int or transient_http_retries not in (0, 1, 2):
            raise ValueError("transient_http_retries must be an integer from 0 to 2")
        if transient_http_retries and (max_attempts != 1 or retry_transport):
            raise ValueError("HTTP retries cannot combine with transport retry policy")
        if reasoning_effort is not None and (
            type(reasoning_effort) is not str or reasoning_effort != "none"
        ):
            raise ValueError("reasoning_effort must be None or 'none'")
        if type(tool_choice_policy) is not str or tool_choice_policy not in {
            "auto", "required-when-tools",
        }:
            raise ValueError("tool_choice_policy must be auto or required-when-tools")
        if on_attempt is not None and not callable(on_attempt):
            raise ValueError("on_attempt must be callable")
        super().__init__(api_key, base_url, model, timeout_s, max_retries=1)
        self.read_timeout_s, self.max_attempts = read_timeout_s, max_attempts
        self.retry_transport, self.completion_cap = retry_transport, completion_cap
        self.reasoning_effort = reasoning_effort
        self.tool_choice_policy = tool_choice_policy
        self.transient_http_retries = transient_http_retries
        self.on_attempt, self.transport = on_attempt, transport

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(min(10.0, self.timeout_s), read=self.read_timeout_s),
                follow_redirects=False, transport=self.transport,
            )
        return self._client

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        if not self.api_key:
            raise SchoolStreamError("School API key is not configured")
        payload = {"model": self.model, "messages": messages, "temperature": 0.2, "stream": True}
        if tools:
            choice = "required" if self.tool_choice_policy == "required-when-tools" else "auto"
            payload.update(tools=tools, tool_choice=choice)
        if self.completion_cap is not None:
            payload["max_tokens"] = self.completion_cap
        if self.reasoning_effort is not None:
            payload["reasoning_effort"] = self.reasoning_effort
        call_id = uuid4().hex
        if self.transient_http_retries:
            return await self._chat_transient_http(payload, call_id)
        for attempt in range(1, self.max_attempts + 1):
            try:
                return await self._attempt(payload, call_id, attempt)
            except httpx.TransportError as error:
                if not self.retry_transport or attempt == self.max_attempts:
                    raise SchoolStreamError(
                        f"School stream transport failed ({type(error).__name__})"
                    ) from None
        raise AssertionError("Unreachable school attempt state")

    async def _chat_transient_http(self, payload: dict, call_id: str) -> LLMResponse:
        """Retry only header failures; all mutable accounting belongs to this call."""
        call_started = time.monotonic()
        deadline = call_started + self.timeout_s
        call_timeout = asyncio.timeout(self.timeout_s)
        try:
            async with call_timeout:
                for attempt in range(1, self.transient_http_retries + 2):
                    row: dict = {}
                    try:
                        result = await self._attempt(
                            payload, call_id, attempt, deadline=deadline,
                            diagnostic=row, emit=False, call_timeout=call_timeout,
                        )
                        if attempt > 1:
                            # Success-only usage is not the complete call's bill. Keep
                            # known attempt usage in the sidecar, never report zero.
                            result = result.model_copy(update={
                                "usage_present": False, "usage_raw": {},
                            })
                        return result
                    except SchoolStreamHTTPError as error:
                        eligible = (error.status_code in (502, 503, 504)
                                    and row.get("decoded_bytes") == 0
                                    and row.get("first_body_byte_s") is None
                                    and row.get("event_count") == 0
                                    and row.get("tool_count") == 0)
                        row["http_retry_eligible"] = eligible
                        if not eligible:
                            row["http_retry_decision"] = "ineligible"
                            raise
                        if attempt > self.transient_http_retries:
                            row["http_retry_decision"] = "retry_limit_reached"
                            raise
                        delay = (5.0, 15.0)[attempt - 1]
                        if deadline - time.monotonic() <= delay:
                            row["http_retry_decision"] = "insufficient_deadline"
                            raise SchoolStreamTimeout(
                                "No call deadline remains for HTTP retry"
                            ) from None
                        row.update(http_retry_decision="backoff_permitted",
                                   http_retry_backoff_planned_s=delay)
                        backoff_started = time.monotonic()
                        try:
                            await asyncio.sleep(delay)
                        except BaseException as error:
                            deadline_cancel = (isinstance(error, asyncio.CancelledError)
                                               and call_timeout.expired())
                            row.update(http_retry_decision="backoff_interrupted",
                                       http_retry_backoff_completed=False,
                                       http_retry_backoff_error_class=(
                                           "SchoolStreamTimeout" if deadline_cancel
                                           else type(error).__name__
                                       ))
                            raise
                        else:
                            row.update(http_retry_decision="backoff_completed",
                                       http_retry_backoff_completed=True)
                        finally:
                            row["http_retry_backoff_elapsed_s"] = (
                                time.monotonic() - backoff_started
                            )
                        if time.monotonic() >= deadline:
                            row["http_retry_decision"] = "deadline_after_backoff"
                            raise SchoolStreamTimeout(
                                "School stream call deadline expired"
                            ) from None
                    except httpx.TransportError as error:
                        # Opting into HTTP retries does not opt into transport retries.
                        raise SchoolStreamError(
                            f"School stream transport failed ({type(error).__name__})"
                        ) from None
                    finally:
                        if row:
                            row["call_elapsed_s"] = time.monotonic() - call_started
                            row["call_deadline_expired"] = (
                                call_timeout.expired() or time.monotonic() >= deadline
                            )
                            if self.on_attempt is not None:
                                self.on_attempt(row)
        except TimeoutError as error:
            raise SchoolStreamTimeout("School stream call deadline expired") from error
        raise AssertionError("Unreachable school HTTP retry state")

    async def _attempt(
        self, payload: dict, call_id: str, attempt: int, *, deadline: float | None = None,
        diagnostic: dict | None = None, emit: bool = True,
        call_timeout: asyncio.Timeout | None = None,
    ) -> LLMResponse:
        started = time.monotonic()

        def clock() -> float:
            return time.monotonic() - started

        assembly = _Assembly(self, clock)
        required_names = (_offered_tool_names(payload.get("tools"))
                          if payload.get("tool_choice") == "required" else None)
        parser = _SSE(assembly, self)
        response = None
        diagnostic = {} if diagnostic is None else diagnostic
        diagnostic.update({"call_id": call_id, "attempt": attempt, "status": "failed",
                      "configured_model": _identifier(self.model), "returned_model": None,
                      "requested_reasoning_effort": self.reasoning_effort,
                      "configured_tool_choice_policy": self.tool_choice_policy,
                      "requested_tool_choice": payload.get("tool_choice"),
                      "offered_tool_count": (len(payload["tools"])
                                             if isinstance(payload.get("tools"), list) else
                                             0 if payload.get("tools") is None else None),
                      "provider_tool_choice_compliance": None,
                      "requested_completion_cap": self.completion_cap,
                      "max_content_chars": self.max_content_chars,
                      "max_argument_chars": self.max_argument_chars,
                      "provider_output_limit_compliance": None,
                      "HTTP_status": None, "headers_s": None, "first_body_byte_s": None,
                      "decoded_bytes": 0, "error_class": None, "partial_discarded": True,
                      "configured_transient_http_retries": self.transient_http_retries,
                      "legacy_max_attempts": self.max_attempts,
                      "legacy_retry_transport": self.retry_transport,
                      "max_http_attempts_per_call": (self.transient_http_retries + 1
                                                     if self.transient_http_retries
                                                     else self.max_attempts),
                      "shared_call_deadline_s": (self.timeout_s if deadline is not None else None),
                      "http_request_started": False, "http_retry_eligible": None,
                      "http_retry_decision": None, "http_retry_backoff_planned_s": None,
                      "http_retry_backoff_elapsed_s": None,
                      "http_retry_backoff_completed": None,
                      "http_retry_backoff_error_class": None,
                      "call_deadline_expired": False})
        remaining = self.timeout_s if deadline is None else deadline - time.monotonic()
        close_budget = min(1.0, max(0.0, remaining) / 5)
        try:
            try:
                if remaining <= 0:
                    raise SchoolStreamTimeout("School stream call deadline expired")
                async with asyncio.timeout(remaining - close_budget):
                    request = self._http().build_request(
                        "POST", f"{self.base_url}/chat/completions", json=payload,
                        headers={"Authorization": f"Bearer {self.api_key}",
                                 "Content-Type": "application/json"},
                    )
                    diagnostic["http_request_started"] = True
                    response = await self._http().send(request, stream=True)
                    diagnostic["headers_s"] = clock()
                    diagnostic["HTTP_status"] = response.status_code
                    if deadline is not None and time.monotonic() >= deadline:
                        raise SchoolStreamTimeout("School stream call deadline expired")
                    if response.status_code != 200:
                        if self.transient_http_retries:
                            raise SchoolStreamHTTPError(response.status_code)
                        raise SchoolStreamError(f"School stream HTTP {response.status_code}")
                    kind = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if kind != "text/event-stream":
                        raise SchoolStreamError("School stream requires text/event-stream")
                    async for chunk in response.aiter_bytes():
                        if deadline is not None and time.monotonic() >= deadline:
                            raise SchoolStreamTimeout("School stream call deadline expired")
                        if chunk and diagnostic["first_body_byte_s"] is None:
                            diagnostic["first_body_byte_s"] = clock()
                        diagnostic["decoded_bytes"] += len(chunk)
                        if diagnostic["decoded_bytes"] > self.max_stream_bytes:
                            raise SchoolStreamError("School stream byte bound exceeded")
                        parser.feed(chunk)
                        if parser.done:
                            break
                    parser.feed(b"", final=True)
                    if not parser.done:
                        raise SchoolStreamError("School stream ended before DONE")
                    if deadline is not None and time.monotonic() >= deadline:
                        raise SchoolStreamTimeout("School stream call deadline expired")
                    result = assembly.response(required_names)
            except TimeoutError as error:
                raise SchoolStreamTimeout("School stream attempt deadline expired") from error
            diagnostic.update(status="completed", partial_discarded=False)
            return result
        except BaseException as error:
            cancelled = isinstance(error, asyncio.CancelledError)
            deadline_cancel = cancelled and call_timeout is not None and call_timeout.expired()
            diagnostic["status"] = "cancelled" if cancelled and not deadline_cancel else "failed"
            diagnostic["error_class"] = (
                "SchoolStreamTimeout" if deadline_cancel else type(error).__name__
            )
            raise
        finally:
            try:
                if response is not None:
                    close_remaining = (self.timeout_s - clock() if deadline is None
                                       else deadline - time.monotonic())
                    async with asyncio.timeout(min(close_budget, max(0.001, close_remaining))):
                        await response.aclose()
            except BaseException as error:
                cancelled = isinstance(error, asyncio.CancelledError)
                deadline_cancel = (cancelled and call_timeout is not None
                                   and call_timeout.expired())
                diagnostic.update(status="cancelled" if cancelled and not deadline_cancel
                                  else "failed", partial_discarded=True,
                                  error_class=("SchoolStreamTimeout" if deadline_cancel
                                               else type(error).__name__))
                raise
            finally:
                diagnostic.update(
                    call_deadline_expired=(
                        (call_timeout is not None and call_timeout.expired())
                        or (deadline is not None and time.monotonic() >= deadline)
                    ),
                    completion_s=clock(), returned_model=_identifier(assembly.model),
                    first_event_s=parser.first_event_s, first_content_s=assembly.first_content_s,
                    first_tool_s=assembly.first_tool_s, event_count=parser.events,
                    finish_reason=assembly.finish, done=parser.done,
                    content_chars=len(assembly.content), reasoning_chars=assembly.reasoning_chars,
                    content_chars_received=assembly.content_chars_received,
                    content_bound_exceeded=assembly.content_bound_exceeded,
                    limit_triggered=("ordinary_content_characters"
                                     if assembly.content_bound_exceeded else
                                     "provider_length_finish" if assembly.finish == "length"
                                     else None),
                    tool_count=len(assembly.tools), usage_raw=dict(assembly.usage),
                    required_tool_choice_satisfied=assembly.required_tool_choice_satisfied,
                    usage_present=all(key in assembly.usage
                                      for key in ("prompt_tokens", "completion_tokens")),
                    returned_call_usage_complete=(
                        diagnostic["status"] == "completed"
                        and not (self.transient_http_retries and attempt > 1)
                        and all(key in assembly.usage
                                for key in ("prompt_tokens", "completion_tokens"))
                    ),
                )
                if emit and self.on_attempt is not None:
                    self.on_attempt(diagnostic)
