"""Offline transport contracts: real fragmented byte streams, never school APIs."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from moumou.eval.school_stream import SchoolStreamClient, SchoolStreamError, SchoolStreamTimeout

MESSAGES = [{"role": "user", "content": "private-request-text"}]
TOOLS = [{"type": "function", "function": {"name": "probe", "parameters": {"type": "object"}}}]


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
        self.started = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        for chunk in self.chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            if isinstance(chunk, asyncio.Event):
                await chunk.wait()
            else:
                yield chunk

    async def aclose(self):
        self.closed = True


def event(delta=None, *, finish=None, usage=None, index=0):
    value = {"model": "returned-e10", "choices": [{"index": index,
             "delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        value["usage"] = usage
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\r\n\r\n").encode()


def done():
    return b"data: [DONE]\r\n\r\n"


def text_response(text="OK", usage=None):
    return event({"content": text}) + event(finish="stop", usage=usage) + done()


def tool(index, *, identity=None, name=None, args=None):
    function = {}
    if name is not None:
        function["name"] = name
    if args is not None:
        function["arguments"] = args
    call = {"index": index, "type": "function", "function": function}
    if identity is not None:
        call["id"] = identity
    return event({"tool_calls": [call]})


def client_for(chunks, **options):
    stream = Chunks(chunks)
    requests, diagnostics = [], []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream; charset=utf-8"},
                              stream=stream)

    client = SchoolStreamClient(api_key="offline-key", base_url="https://school.invalid",
                                model="requested-e7", transport=httpx.MockTransport(respond),
                                on_attempt=diagnostics.append, **options)
    return client, stream, requests, diagnostics


async def test_fragmented_utf8_crlf_multiline_usage_and_no_default_output_cap():
    first = json.dumps({"model": "returned-e10", "choices": [{"index": 0,
                       "delta": {"content": "猫🙂"}, "finish_reason": None}]},
                       ensure_ascii=False)
    # SSE joins data lines with a newline; this is legal JSON whitespace.
    first = first.replace(', "choices"', ',\n"choices"')
    wire = b"\xef\xbb\xbf: keepalive\r\n\r\n" + b"".join(
        b"data: " + line.encode() + b"\r\n" for line in first.splitlines()
    ) + b"\r\n" + event(finish="stop") + (
        b'data: {"choices":[],"usage":{"prompt_tokens":101,"completion_tokens":34,'
        b'"total_tokens":135,"prompt_cache_hit_tokens":5}}\r\n\r\n'
    ) + done()
    client, stream, requests, diagnostics = client_for([wire[i:i + 1] for i in range(len(wire))])
    try:
        result = await client.chat(MESSAGES)
    finally:
        await client.aclose()
    assert result.content == "猫🙂" and result.model == "returned-e10"
    assert result.usage_present is True and result.tokens_in == 101 and result.tokens_out == 34
    assert result.cache_hit_tokens == 5 and result.usage_raw["total_tokens"] == 135
    payload = json.loads(requests[0].content)
    assert payload["stream"] is True and payload["model"] == "requested-e7"
    assert "max_tokens" not in payload and "stream_options" not in payload
    assert "reasoning_effort" not in payload
    assert len(requests) == 1 and stream.closed
    record = diagnostics[0]
    assert record["requested_reasoning_effort"] is None
    assert record["status"] == "completed" and record["partial_discarded"] is False
    assert record["configured_model"] == "requested-e7"
    assert record["returned_model"] == "returned-e10"
    assert 0 <= record["headers_s"] <= record["first_body_byte_s"]
    assert record["first_body_byte_s"] <= record["first_content_s"] <= record["completion_s"]


async def test_interleaved_tool_arguments_are_assembled_only_after_terminal_done():
    chunks = [tool(0, identity="a", name="pro", args='{"x":'),
              tool(1, identity="b", name="other", args='{"y":'),
              tool(0, name="be", args='"private-tool-argument"}'), tool(1, args="2}"),
              event(finish="tool_calls", usage={"prompt_tokens":390, "completion_tokens":67}),
              done()]
    client, stream, requests, diagnostics = client_for(
        chunks, completion_cap=256, reasoning_effort="none",
    )
    try:
        result = await client.chat(MESSAGES, TOOLS)
    finally:
        await client.aclose()
    assert [(call.id, call.name, call.args) for call in result.tool_calls] == [
        ("a", "probe", {"x": "private-tool-argument"}), ("b", "other", {"y": 2}),
    ]
    assert stream.closed and result.usage_present is True
    payload = json.loads(requests[0].content)
    assert payload["tools"] == TOOLS and payload["tool_choice"] == "auto"
    assert payload["max_tokens"] == 256
    assert payload["reasoning_effort"] == "none" and "extra_body" not in payload
    assert diagnostics[0]["requested_reasoning_effort"] == "none"
    saved = json.dumps(diagnostics)
    for forbidden in ("private-request-text", "private-tool-argument", "offline-key",
                      "Authorization"):
        assert forbidden not in saved
    assert diagnostics[0]["tool_count"] == 2


@pytest.mark.parametrize("wire", [
    event({"content": "partial"}) + event(finish="stop"),
    event({"content": "partial"}) + done(),
    event({"content": "partial"}) + event(finish="length") + done(),
    event({"content": "partial"}) + event(finish="content_filter") + done(),
    event({"content": "partial"}) + b'data: {"error":{"message":"private"}}\n\n',
    event({"content": "partial"}) + b"data: not-json\n\n",
    event({"content": "partial"}) + b"data: \xff\n\n",
    event({"content": "partial"}) + event(finish="stop") + done() + b"data: {}",
    event({"content": "partial"}, index=1) + event(finish="stop") + done(),
    event({"content": "partial"}, index=False) + event(finish="stop") + done(),
    b'data: {"choices":[{"index":0,"delta":{}},{"index":1,"delta":{}}]}\n\n',
])
async def test_partial_or_invalid_stream_never_returns_a_response_or_retries(wire):
    client, stream, requests, diagnostics = client_for(
        [wire], max_attempts=2, retry_transport=True,
    )
    try:
        with pytest.raises(SchoolStreamError):
            await client.chat(MESSAGES)
    finally:
        await client.aclose()
    assert len(requests) == 1 and stream.closed
    assert diagnostics[0]["status"] == "failed" and diagnostics[0]["partial_discarded"] is True


@pytest.mark.parametrize("first", [
    tool(0, identity="a", name="probe", args='{"x":'),
    tool(0, identity="a", name="probe", args="[]"),
    tool(0, identity="a", name="probe", args='{"x":1,"x":2}'),
    tool(0, identity="a", name="probe", args='{"x":NaN}'),
    tool(0, identity="a", name="probe", args='{"x":1e309}'),
    tool(0, name="probe", args="{}"),
    tool(1, identity="a", name="probe", args="{}"),
    tool(0, identity="a", name="probe", args="{}") + tool(0, identity="b", args=""),
    tool(0, identity="a", name="probe", args="{}") + tool(1, identity="a", name="probe", args="{}"),
])
async def test_invalid_or_ambiguous_final_tool_arguments_are_not_executable(first):
    client, stream, _requests, diagnostics = client_for([first, event(finish="tool_calls"), done()])
    try:
        with pytest.raises(SchoolStreamError):
            await client.chat(MESSAGES, TOOLS)
    finally:
        await client.aclose()
    assert stream.closed and diagnostics[0]["partial_discarded"] is True


def escaped_event(delta):
    value = {"model": "returned-e10", "choices": [{"index": 0, "delta": delta}]}
    return ("data: " + json.dumps(value, ensure_ascii=True) + "\n\n").encode()


@pytest.mark.parametrize("delta", [
    {"content": "\ud800"},
    {"tool_calls": [{"index": 0, "id": "a", "function": {
        "name": "probe", "arguments": '{"nested":{"value":"\\ud800"}}',
    }}]},
    {"tool_calls": [{"index": 0, "id": "a", "function": {
        "name": "probe", "arguments": '{"nested":{"\\ud800":"value"}}',
    }}]},
    {"tool_calls": [{"index": 0, "id": "a", "function": {
        "name": "\ud800", "arguments": "{}",
    }}]},
])
async def test_escaped_lone_surrogates_fail_before_result_or_utf8_journal(delta):
    finish = "tool_calls" if "tool_calls" in delta else "stop"
    client, stream, _requests, diagnostics = client_for([
        escaped_event(delta), event(finish=finish), done(),
    ])
    try:
        with pytest.raises(SchoolStreamError) as error:
            await client.chat(MESSAGES, TOOLS)
    finally:
        await client.aclose()
    str(error.value).encode("utf-8")
    json.dumps(diagnostics, ensure_ascii=False).encode("utf-8")
    assert stream.closed and diagnostics[0]["partial_discarded"] is True


async def test_escaped_surrogate_pair_is_valid_emoji_in_content_and_nested_arguments():
    delta = {"content": "😀", "tool_calls": [{"index": 0, "id": "a", "function": {
        "name": "probe", "arguments": '{"nested":{"emoji":"\\ud83d\\ude00"}}',
    }}]}
    client, _stream, _requests, diagnostics = client_for([
        escaped_event(delta), event(finish="tool_calls"), done(),
    ])
    try:
        result = await client.chat(MESSAGES, TOOLS)
    finally:
        await client.aclose()
    assert result.content == "😀" and result.tool_calls[0].args == {"nested": {"emoji": "😀"}}
    result.model_dump_json().encode("utf-8")
    assert diagnostics[0]["status"] == "completed"


@pytest.mark.parametrize("options,wire", [
    ({"max_stream_bytes": 10}, text_response()),
    ({"max_event_bytes": 20}, text_response()),
    ({"max_events": 1}, text_response()),
    ({"max_content_chars": 2}, text_response("long")),
    ({"max_argument_chars": 2}, tool(0, identity="a", name="probe", args='{"x":1}')),
    ({"max_tools": 1}, tool(1, identity="a", name="probe", args="{}")),
])
async def test_byte_event_content_and_tool_limits_are_enforced_before_return(options, wire):
    client, stream, _requests, diagnostics = client_for([wire], **options)
    try:
        with pytest.raises(SchoolStreamError):
            await client.chat(MESSAGES)
    finally:
        await client.aclose()
    assert stream.closed and diagnostics[0]["partial_discarded"]


@pytest.mark.parametrize("usage,measured,tokens_in,tokens_out", [
    (None, False, 0, 0),
    ({"prompt_tokens": 5}, False, 5, 0),
    ({"prompt_tokens": True, "completion_tokens": -1}, False, 0, 0),
    ({"prompt_tokens": 0, "completion_tokens": 0}, True, 0, 0),
])
async def test_missing_usage_remains_unknown_and_measured_zero_is_distinct(
    usage, measured, tokens_in, tokens_out,
):
    client, _stream, _requests, diagnostics = client_for([text_response(usage=usage)])
    try:
        result = await client.chat(MESSAGES)
    finally:
        await client.aclose()
    assert result.usage_present is measured
    assert (result.tokens_in, result.tokens_out) == (tokens_in, tokens_out)
    assert diagnostics[0]["usage_present"] is measured


async def test_cancellation_closes_response_and_never_returns_partial_tool_or_retries():
    gate = asyncio.Event()
    client, stream, requests, diagnostics = client_for(
        [tool(0, identity="a", name="probe", args='{"x":'), gate],
        max_attempts=2, retry_transport=True,
    )
    task = asyncio.create_task(client.chat(MESSAGES, TOOLS))
    await asyncio.wait_for(stream.started.wait(), 1)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await client.aclose()
    assert stream.closed and len(requests) == 1
    assert diagnostics[0]["status"] == "cancelled" and diagnostics[0]["partial_discarded"]


async def test_overall_deadline_is_bounded_and_not_a_transport_retry_policy():
    client, stream, requests, diagnostics = client_for(
        [event({"content": "partial"}), asyncio.Event()], timeout_s=0.2,
        read_timeout_s=0.05, max_attempts=2, retry_transport=True,
    )
    try:
        assert client._http().timeout.read == 0.05
        with pytest.raises(SchoolStreamTimeout):
            await client.chat(MESSAGES)
    finally:
        await client.aclose()
    assert stream.closed and len(requests) == 1
    assert diagnostics[0]["partial_discarded"]


@pytest.mark.parametrize("effort", [None, "none"])
async def test_only_explicit_transport_retry_starts_one_fresh_second_assembly(effort):
    streams = [Chunks([tool(0, identity="old", name="probe", args='{"old":'),
                       httpx.ReadError("private-transport-message")]),
               Chunks([text_response("fresh", {"prompt_tokens":9, "completion_tokens":2})])]
    requests, diagnostics = [], []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=streams[len(requests) - 1])

    client = SchoolStreamClient(api_key="offline", base_url="https://school.invalid",
                                transport=httpx.MockTransport(respond), max_attempts=2,
                                retry_transport=True, on_attempt=diagnostics.append,
                                reasoning_effort=effort)
    try:
        result = await client.chat(MESSAGES, TOOLS)
    finally:
        await client.aclose()
    assert result.content == "fresh" and not result.tool_calls
    assert len(requests) == 2 and all(stream.closed for stream in streams)
    assert all(json.loads(request.content)["stream"] for request in requests)
    payloads = [json.loads(request.content) for request in requests]
    assert all(payload.get("reasoning_effort") == effort for payload in payloads)
    assert all(("reasoning_effort" in payload) is (effort is not None) for payload in payloads)
    assert [row["requested_reasoning_effort"] for row in diagnostics] == [effort, effort]
    assert [row["attempt"] for row in diagnostics] == [1, 2]
    assert diagnostics[0]["call_id"] == diagnostics[1]["call_id"]
    assert diagnostics[0]["partial_discarded"] and not diagnostics[1]["partial_discarded"]
    assert diagnostics[0]["usage_present"] is False
    assert "private-transport-message" not in json.dumps(diagnostics)


async def test_transport_failure_default_has_no_retry_and_safe_error_text():
    client, stream, requests, diagnostics = client_for([
        event({"content": "private-output"}), httpx.ReadError("secret-error-content"),
    ])
    try:
        with pytest.raises(SchoolStreamError) as error:
            await client.chat(MESSAGES)
    finally:
        await client.aclose()
    assert "secret-error-content" not in str(error.value)
    assert len(requests) == 1 and stream.closed
    assert diagnostics[0]["error_class"] == "ReadError"


@pytest.mark.parametrize("status,kind", [(504, "text/event-stream"), (200, "application/json")])
async def test_http_error_and_json_downgrade_are_rejected_without_retry(status, kind):
    requests, diagnostics = [], []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, headers={"content-type": kind}, text="private-body-secret")

    client = SchoolStreamClient(api_key="offline", base_url="https://school.invalid",
                                transport=httpx.MockTransport(respond), max_attempts=2,
                                retry_transport=True, on_attempt=diagnostics.append)
    try:
        with pytest.raises(SchoolStreamError) as error:
            await client.chat(MESSAGES)
    finally:
        await client.aclose()
    assert len(requests) == 1 and "private-body-secret" not in str(error.value)
    assert "private-body-secret" not in json.dumps(diagnostics)


@pytest.mark.parametrize("effort", [None, "none"])
async def test_concurrent_calls_keep_tool_buffers_timing_and_diagnostics_separate(effort):
    gate = asyncio.Event()
    streams, diagnostics, payloads = [], [], []

    def respond(request):
        payloads.append(json.loads(request.content))
        number = len(streams) + 1
        stream = Chunks([tool(0, identity=f"id-{number}", name="probe", args='{"n":'), gate,
                         tool(0, args=f"{number}}}"), event(finish="tool_calls"), done()])
        streams.append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    client = SchoolStreamClient(api_key="offline", base_url="https://school.invalid",
                                transport=httpx.MockTransport(respond),
                                on_attempt=diagnostics.append, reasoning_effort=effort)
    tasks = [asyncio.create_task(client.chat(MESSAGES, TOOLS)) for _ in range(2)]
    try:
        async with asyncio.timeout(1):
            while len(streams) < 2:
                await asyncio.sleep(0)
            await asyncio.gather(*(stream.started.wait() for stream in streams))
        gate.set()
        results = await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.aclose()
    assert [result.tool_calls[0].args for result in results] == [{"n": 1}, {"n": 2}]
    assert [result.tool_calls[0].id for result in results] == ["id-1", "id-2"]
    assert len({record["call_id"] for record in diagnostics}) == 2
    assert all(record["attempt"] == 1 and record["status"] == "completed" for record in diagnostics)
    assert all(stream.closed for stream in streams)
    assert all(payload.get("reasoning_effort") == effort for payload in payloads)
    assert all(("reasoning_effort" in payload) is (effort is not None) for payload in payloads)
    assert all(record["requested_reasoning_effort"] == effort for record in diagnostics)


@pytest.mark.parametrize("effort", ["", "low", "high", "max", True, False, 0, {},
                                   {"effort": "none"}])
def test_unsupported_reasoning_controls_fail_before_config_loading(monkeypatch, effort):
    def no_config():
        raise AssertionError("Invalid reasoning controls must not read configuration")

    monkeypatch.setattr("moumou.config.load_dotenv", no_config)
    with pytest.raises(ValueError, match="reasoning_effort"):
        SchoolStreamClient(api_key="offline", reasoning_effort=effort)


@pytest.mark.parametrize("options", [
    {"max_attempts": 3}, {"max_attempts": 2}, {"max_attempts": True},
    {"completion_cap": 0}, {"completion_cap": True}, {"completion_cap": -1},
    {"timeout_s": 0}, {"read_timeout_s": float("inf")}, {"max_events": 0},
])
def test_invalid_limits_and_implicit_retry_policy_are_rejected(options):
    with pytest.raises(ValueError):
        SchoolStreamClient(api_key="offline", **options)
