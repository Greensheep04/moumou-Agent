"""Offline contracts for explicit required-when-tools requests and strict assembly."""

from __future__ import annotations

import asyncio
import builtins
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_quality_tools_cli import exercise, forbid, read, single_case
from test_school_stream import MESSAGES, TOOLS, Chunks, client_for, done, event, text_response, tool

from moumou.config import LLMSettings
from moumou.contracts import Acceptance, Budget, ToolResult
from moumou.eval import cli
from moumou.eval import school_stream as stream_module
from moumou.eval.human import RecordingLLM
from moumou.eval.school_stream import (
    SchoolStreamClient,
    SchoolStreamError,
    SchoolStreamHTTPError,
    SchoolStreamTimeout,
)
from moumou.orchestrator import Engine, Settings
from moumou.tools import ToolRegistry
from moumou.tools.builtin import SubmitResultTool


@pytest.mark.parametrize("choice", ["auto", "required-when-tools"])
def test_choice_with_buffered_refuses_before_keys_and_output(tmp_path, monkeypatch, choice):
    monkeypatch.setattr(LLMSettings, "from_env", classmethod(forbid))
    monkeypatch.setattr("moumou.config.load_dotenv", forbid)
    output = tmp_path / "no-session"
    with pytest.raises(SystemExit) as error:
        cli.main(["--out", str(output), "--llm-tool-choice", choice])
    assert error.value.code == 2
    assert not output.exists()


@pytest.mark.parametrize("choice", [True, 1, "required", "sometimes", ["auto"]])
async def test_programmatic_invalid_choice_refuses_before_keys_or_output(
    tmp_path, monkeypatch, choice,
):
    bank, case, _roles, _sha = single_case(tmp_path)
    output = tmp_path / "no-session"
    args = cli.parse_args(["--tasks", str(bank), "--out", str(output),
                           "--llm-transport", "stream"])
    args.llm_tool_choice = choice
    monkeypatch.setattr(LLMSettings, "from_env", classmethod(forbid))
    with pytest.raises(ValueError, match="tool choice"):
        await cli.run_suite(args, [case])
    assert not output.exists()


@pytest.mark.parametrize("choice", ["auto", "required-when-tools"])
def test_plan_records_choice_without_optional_client_or_environment(
    tmp_path, monkeypatch, capsys, choice,
):
    bank, _case, _roles, _sha = single_case(tmp_path)
    output = tmp_path / "plan-no-output"
    original_import = builtins.__import__

    def checked_import(name, *args, **kwargs):
        if name == "moumou.eval.school_stream":
            raise AssertionError("--plan imported the optional client")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(LLMSettings, "from_env", classmethod(forbid))
    monkeypatch.setattr("moumou.config.load_dotenv", forbid)
    monkeypatch.setattr("httpx.AsyncClient", forbid)
    monkeypatch.setattr("httpx.Client", forbid)
    monkeypatch.setattr(builtins, "__import__", checked_import)
    assert cli.main(["--tasks", str(bank), "--out", str(output), "--plan",
                     "--llm-transport", "stream", "--llm-tool-choice", choice]) == 0
    assert json.loads(capsys.readouterr().out)["llm_tool_choice"] == choice
    assert not output.exists()


def test_omission_keeps_old_constructor_options_absent():
    buffered = cli.parse_args([])
    stream = cli.parse_args(["--llm-transport", "stream"])
    assert buffered.llm_transport == "buffered"
    assert buffered.llm_tool_choice is stream.llm_tool_choice is None
    assert cli._stream_tool_choice_options(buffered, "buffered") == {}
    assert cli._stream_tool_choice_options(stream, "stream") == {}


async def test_cli_factory_manifest_and_sidecar_record_explicit_policy(tmp_path, monkeypatch):
    original_parse = cli.parse_args

    def with_policy(options):
        return original_parse([*options, "--llm-tool-choice", "required-when-tools"])

    monkeypatch.setattr(cli, "parse_args", with_policy)
    output, entry, constructors, _attempts = await exercise(
        tmp_path, monkeypatch, transport="stream",
    )
    assert constructors[0]["tool_choice_policy"] == "required-when-tools"
    for item in (read(output / "test.json")["model"],
                 read(output / entry["scenario_id"] / "stream-transport.json")):
        assert item["tool_choice_policy"] == "required-when-tools"
        assert item["tool_choice_explicit"] is True
        assert item["provider_tool_choice_compliance"] is None


async def test_default_stream_factory_still_omits_new_keyword(tmp_path, monkeypatch):
    output, entry, constructors, _attempts = await exercise(
        tmp_path, monkeypatch, transport="stream",
    )
    assert "tool_choice_policy" not in constructors[0]
    model = read(output / "test.json")["model"]
    assert model["tool_choice_policy"] == "auto"
    assert model["tool_choice_explicit"] is False
    assert read(output / entry["scenario_id"] / "stream-transport.json")[
        "tool_choice_policy"
    ] == "auto"


@pytest.mark.parametrize("policy", [None, True, 1, "required", "invalid"])
def test_invalid_direct_client_policy_refuses(policy):
    with pytest.raises(ValueError, match="tool_choice_policy"):
        SchoolStreamClient(api_key="offline", tool_choice_policy=policy)


@pytest.mark.parametrize("offered", [None, []])
async def test_required_policy_preserves_tool_free_plain_format_without_fallback(offered):
    client, stream, requests, diagnostics = client_for(
        [text_response("A complete plain response")],
        tool_choice_policy="required-when-tools",
    )
    try:
        result = await client.chat(MESSAGES, offered)
    finally:
        await client.aclose()
    payload = json.loads(requests[0].content)
    assert "tools" not in payload and "tool_choice" not in payload
    assert result.content == "A complete plain response" and stream.closed
    observed = diagnostics[0]
    assert observed["configured_tool_choice_policy"] == "required-when-tools"
    assert observed["requested_tool_choice"] is None and observed["offered_tool_count"] == 0
    assert observed["required_tool_choice_satisfied"] is None
    assert observed["usage_present"] is False
    assert observed["provider_tool_choice_compliance"] is None


async def test_fragmented_required_tool_is_returned_only_after_valid_finish_and_done():
    wire = (tool(0, identity="a", name="pr", args='{"x":')
            + tool(0, name="obe", args="1}") + event(finish="tool_calls") + done())
    client, stream, requests, diagnostics = client_for(
        [wire[index:index + 1] for index in range(len(wire))],
        tool_choice_policy="required-when-tools",
    )
    try:
        result = await client.chat(MESSAGES, TOOLS)
    finally:
        await client.aclose()
    assert len(requests) == 1 and stream.closed
    assert json.loads(requests[0].content)["tool_choice"] == "required"
    assert result.tool_calls[0].name == "probe" and result.tool_calls[0].args == {"x": 1}
    observed = diagnostics[0]
    assert observed["requested_tool_choice"] == "required"
    assert observed["offered_tool_count"] == 1
    assert observed["required_tool_choice_satisfied"] is True
    assert observed["provider_tool_choice_compliance"] is None
    assert observed["usage_present"] is False


@pytest.mark.parametrize("wire,satisfied", [
    (text_response("ordinary text despite a required request"), False),
    (tool(0, identity="a", name="unknown", args="{}")
     + event(finish="tool_calls") + done(), False),
    (tool(0, identity="a", name="probe", args='{"x":')
     + event(finish="tool_calls") + done(), None),
    (tool(0, identity="a", name="probe", args="{}")
     + event(finish="tool_calls"), None),
    (tool(0, identity="a", name="probe", args="{}")
     + event(finish="length") + done(), None),
])
async def test_required_non_tool_unknown_or_partial_response_never_falls_back(wire, satisfied):
    client, stream, requests, diagnostics = client_for(
        [wire], tool_choice_policy="required-when-tools",
        max_attempts=2, retry_transport=True,
    )
    try:
        with pytest.raises(SchoolStreamError):
            await client.chat(MESSAGES, TOOLS)
    finally:
        await client.aclose()
    assert len(requests) == 1 and stream.closed
    assert json.loads(requests[0].content)["tool_choice"] == "required"
    observed = diagnostics[0]
    assert observed["status"] == "failed" and observed["partial_discarded"] is True
    assert observed["required_tool_choice_satisfied"] is satisfied
    assert observed["usage_present"] is False
    assert observed["provider_tool_choice_compliance"] is None


@pytest.mark.parametrize("offered", [
    [{"type": "function", "function": {"name": "bad name"}}],
    [{"type": "function", "function": {"name": "probe"}}] * 2,
    [{"type": "function", "function": {}}],
])
async def test_ambiguous_required_offerings_refuse_before_http(offered):
    client, _stream, requests, diagnostics = client_for(
        [text_response()], tool_choice_policy="required-when-tools",
    )
    try:
        with pytest.raises(SchoolStreamError, match="function"):
            await client.chat(MESSAGES, offered)
    finally:
        await client.aclose()
    assert requests == [] and diagnostics == []


@pytest.mark.parametrize("swapped", [False, True])
async def test_concurrent_required_requests_keep_their_own_offered_names(swapped):
    release = asyncio.Event()
    streams, requests, diagnostics = [], [], []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        name = body["tools"][0]["function"]["name"]
        emitted = {"alpha": "beta", "beta": "alpha"}[name] if swapped else name
        stream = Chunks([tool(0, identity=name, name=emitted, args='{"value":'),
                         release, tool(0, args="1}"), event(finish="tool_calls"), done()])
        streams.append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    client = SchoolStreamClient(api_key="offline", base_url="https://school.invalid",
                                tool_choice_policy="required-when-tools",
                                transport=httpx.MockTransport(respond),
                                on_attempt=diagnostics.append)
    jobs = [asyncio.create_task(client.chat(MESSAGES, [{"type": "function", "function": {
        "name": name, "parameters": {"type": "object"},
    }}])) for name in ("alpha", "beta")]
    try:
        async with asyncio.timeout(2):
            while len(streams) < 2:
                await asyncio.sleep(0)
            await asyncio.gather(*(stream.started.wait() for stream in streams))
            release.set()
            results = await asyncio.gather(*jobs, return_exceptions=True)
    finally:
        for job in jobs:
            if not job.done():
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await client.aclose()
    assert len(requests) == 2 and all(s.closed for s in streams)
    assert all(body["tool_choice"] == "required" for body in requests)
    if swapped:
        assert all(isinstance(result, SchoolStreamError) for result in results)
        assert all(row["required_tool_choice_satisfied"] is False for row in diagnostics)
    else:
        assert [result.tool_calls[0].name for result in results] == ["alpha", "beta"]
        assert all(row["required_tool_choice_satisfied"] is True for row in diagnostics)


class RetryClock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []
        self.sleep_error = None
        self.sleep_extra = 0.0

    def now(self):
        return self.value

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.sleep_error is not None:
            raise self.sleep_error
        self.value += seconds + self.sleep_extra


def http_retry_client(monkeypatch, replies, *, clock=None, **options):
    """Only the candidate's time/sleep are replaced; loop timers stay real."""
    clock = clock or RetryClock()
    requests, diagnostics, streams = [], [], []
    monkeypatch.setattr(stream_module, "time", SimpleNamespace(monotonic=clock.now))
    monkeypatch.setattr(stream_module, "asyncio", SimpleNamespace(
        timeout=asyncio.timeout, sleep=clock.sleep, CancelledError=asyncio.CancelledError,
    ))

    def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) > len(replies):
            raise AssertionError("Unexpected extra HTTP attempt or fallback")
        status, chunks, elapsed = replies[len(requests) - 1]
        clock.value += elapsed
        if isinstance(status, BaseException):
            raise status
        stream = Chunks(chunks)
        streams.append(stream)
        return httpx.Response(status, headers={"content-type": "text/event-stream"},
                              stream=stream)

    client = SchoolStreamClient(
        api_key="offline", base_url="https://school.invalid",
        transport=httpx.MockTransport(respond), on_attempt=diagnostics.append,
        **options,
    )
    return client, requests, diagnostics, streams, clock


@pytest.mark.parametrize("value", [0, 1, 2])
def test_explicit_http_retries_refuse_buffered_before_environment(tmp_path, monkeypatch, value):
    monkeypatch.setattr(LLMSettings, "from_env", classmethod(forbid))
    output = tmp_path / "no-session"
    with pytest.raises(SystemExit) as error:
        cli.main(["--out", str(output), "--llm-transient-http-retries", str(value)])
    assert error.value.code == 2 and not output.exists()


@pytest.mark.parametrize("value", [True, -1, 3, 1.0, "1", [1]])
async def test_invalid_programmatic_http_retries_refuse_before_keys(
    tmp_path, monkeypatch, value,
):
    bank, case, _roles, _sha = single_case(tmp_path)
    output = tmp_path / "no-session"
    args = cli.parse_args(["--tasks", str(bank), "--out", str(output),
                           "--llm-transport", "stream"])
    args.llm_transient_http_retries = value
    monkeypatch.setattr(LLMSettings, "from_env", classmethod(forbid))
    with pytest.raises(ValueError, match="HTTP retries"):
        await cli.run_suite(args, [case])
    assert not output.exists()


def test_retry_plan_reports_requested_and_maximum_without_keys(tmp_path, monkeypatch, capsys):
    bank, _case, _roles, _sha = single_case(tmp_path)
    monkeypatch.setattr(LLMSettings, "from_env", classmethod(forbid))
    assert cli.main(["--tasks", str(bank), "--plan", "--llm-transport", "stream",
                     "--llm-transient-http-retries", "2"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["llm_transient_http_retries"] == 2
    assert plan["llm_max_http_attempts_per_call"] == 3
    assert plan["llm_http_retry_backoff_s"] == [5, 15]


async def test_retry_factory_manifest_sidecar_and_default_omission(tmp_path, monkeypatch):
    original_parse = cli.parse_args

    def retries(options):
        return original_parse([*options, "--llm-transient-http-retries", "2"])

    monkeypatch.setattr(cli, "parse_args", retries)
    output, entry, constructors, _attempts = await exercise(
        tmp_path, monkeypatch, transport="stream",
    )
    assert constructors[0]["transient_http_retries"] == 2
    assert constructors[0]["max_attempts"] == 1
    for observed in (read(output / "test.json")["model"],
                     read(output / entry["scenario_id"] / "stream-transport.json")):
        assert observed["transient_http_retries"] == 2
        assert observed["transient_http_retries_explicit"] is True
        assert observed["http_retry_backoff_s"] == [5, 15]
        assert observed["http_retry_shared_deadline_s"] > 0
    assert read(output / "test.json")["model"]["max_attempts"] == 3
    assert read(output / entry["scenario_id"] / "stream-transport.json")[
        "max_attempts_per_call"
    ] == 3


async def test_retry_omission_does_not_break_old_constructor_shim(tmp_path, monkeypatch):
    output, _entry, constructors, _attempts = await exercise(
        tmp_path, monkeypatch, transport="stream",
    )
    assert "transient_http_retries" not in constructors[0]
    assert cli.parse_args([]).llm_transient_http_retries is None
    assert cli._stream_http_retry_options(cli.parse_args([]), "buffered") == {}
    assert read(output / "test.json")["model"]["transient_http_retries"] == 0


@pytest.mark.parametrize("options", [
    {"transient_http_retries": True}, {"transient_http_retries": -1},
    {"transient_http_retries": 3}, {"transient_http_retries": 1.0},
    {"transient_http_retries": 1, "max_attempts": 2, "retry_transport": True},
    {"transient_http_retries": 1, "retry_transport": True},
])
def test_retry_invalid_or_combined_policy_refuses_before_env(monkeypatch, options):
    monkeypatch.setattr("moumou.config.load_dotenv", forbid)
    with pytest.raises(ValueError):
        SchoolStreamClient(api_key="offline", **options)


@pytest.mark.parametrize("status", [502, 503, 504])
async def test_only_pre_body_gateway_failure_retries_without_fallback(monkeypatch, status):
    usage = {"prompt_tokens": 19, "completion_tokens": 7}
    complete = tool(0, identity="a", name="probe", args="{}")
    complete += event(finish="tool_calls", usage=usage) + done()
    client, requests, rows, streams, clock = http_retry_client(
        monkeypatch, [(status, [b"never read or dispatched"], 0), (200, [complete], 0)],
        transient_http_retries=2, tool_choice_policy="required-when-tools",
    )
    try:
        result = await client.chat(MESSAGES, TOOLS)
    finally:
        await client.aclose()
    assert len(requests) == 2 and requests[0] == requests[1]
    assert all(body["tool_choice"] == "required" for body in requests)
    assert clock.sleeps == [5] and all(s.closed for s in streams)
    assert not streams[0].started.is_set()
    assert result.tool_calls[0].name == "probe"
    assert result.usage_present is False and result.usage_raw == {}
    assert result.tokens_in == 19 and result.tokens_out == 7
    assert rows[0]["decoded_bytes"] == 0 and rows[0]["usage_present"] is False
    assert rows[0]["http_retry_eligible"] is True
    assert rows[0]["http_retry_decision"] == "backoff_completed"
    assert rows[0]["http_retry_backoff_planned_s"] == 5
    assert rows[0]["http_retry_backoff_elapsed_s"] == 5
    assert rows[0]["http_retry_backoff_completed"] is True
    assert rows[1]["usage_present"] is True and rows[1]["usage_raw"] == usage
    assert rows[1]["returned_call_usage_complete"] is False
    assert [row["attempt"] for row in rows] == [1, 2]
    assert len({row["call_id"] for row in rows}) == 1


@pytest.mark.parametrize("retries", [0, 1, 2])
async def test_http_attempt_cap_is_exact_and_default_is_one(monkeypatch, retries):
    options = {"transient_http_retries": retries} if retries else {}
    client, requests, rows, _streams, clock = http_retry_client(
        monkeypatch, [(502, [], 0)] * 4, **options,
    )
    try:
        with pytest.raises(SchoolStreamError) as error:
            await client.chat(MESSAGES, TOOLS)
    finally:
        await client.aclose()
    assert len(requests) == len(rows) == retries + 1
    assert clock.sleeps == [5, 15][:retries]
    assert all(row["http_request_started"] for row in rows)
    assert all(row["usage_present"] is False and row["usage_raw"] == {} for row in rows)
    if retries:
        assert rows[-1]["http_retry_decision"] == "retry_limit_reached"
        assert type(error.value) is SchoolStreamHTTPError
    else:
        assert type(error.value) is SchoolStreamError
        assert rows[-1]["error_class"] == "SchoolStreamError"


@pytest.mark.parametrize("status", [400, 401, 403, 408, 429, 500, 501, 505])
async def test_noneligible_status_never_retries(monkeypatch, status):
    client, requests, rows, _streams, clock = http_retry_client(
        monkeypatch, [(status, [], 0)], transient_http_retries=2,
    )
    try:
        with pytest.raises(SchoolStreamHTTPError):
            await client.chat(MESSAGES)
    finally:
        await client.aclose()
    assert len(requests) == 1 and clock.sleeps == []
    assert rows[0]["http_retry_decision"] == "ineligible"


@pytest.mark.parametrize("wire,extra", [
    (tool(0, identity="a", name="probe", args='{"unfinished":'), {}),
    (tool(0, identity="a", name="probe", args="{}") + event(finish="tool_calls"), {}),
    (text_response("plain text despite required"), {}),
    (event({"content": "partial"}) + event(finish="length") + done(), {}),
    (b"data: not-json\n\n", {}),
    (text_response("12345"), {"max_content_chars": 4}),
])
async def test_http_200_protocol_and_bounds_never_retry_or_dispatch(monkeypatch, wire, extra):
    client, requests, rows, _streams, clock = http_retry_client(
        monkeypatch, [(200, [wire], 0)], transient_http_retries=2,
        tool_choice_policy="required-when-tools", **extra,
    )
    try:
        with pytest.raises(SchoolStreamError):
            await client.chat(MESSAGES, TOOLS)
    finally:
        await client.aclose()
    assert len(requests) == 1 and clock.sleeps == []
    assert rows[0]["partial_discarded"] is True


async def test_transport_error_does_not_gain_http_retry_policy(monkeypatch):
    client, requests, _rows, _streams, clock = http_retry_client(
        monkeypatch, [(httpx.ConnectError("private diagnostic"), [], 0)],
        transient_http_retries=2,
    )
    try:
        with pytest.raises(SchoolStreamError, match="transport failed"):
            await client.chat(MESSAGES)
    finally:
        await client.aclose()
    assert len(requests) == 1 and clock.sleeps == []


async def test_deadline_spans_prior_requests_and_backoff(monkeypatch):
    client, requests, rows, _streams, clock = http_retry_client(
        monkeypatch, [(502, [], 50), (503, [], 115)], transient_http_retries=2,
    )
    try:
        with pytest.raises(SchoolStreamTimeout):
            await client.chat(MESSAGES)
    finally:
        await client.aclose()
    assert clock.value == 170 and clock.sleeps == [5] and len(requests) == 2
    assert rows[-1]["http_retry_decision"] == "insufficient_deadline"
    assert rows[-1]["http_retry_backoff_completed"] is None


async def test_overslept_backoff_cannot_start_another_http_request(monkeypatch):
    clock = RetryClock()
    clock.sleep_extra = 180
    client, requests, rows, _streams, _clock = http_retry_client(
        monkeypatch, [(502, [], 0)], clock=clock, transient_http_retries=2,
    )
    try:
        with pytest.raises(SchoolStreamTimeout):
            await client.chat(MESSAGES)
    finally:
        await client.aclose()
    assert len(requests) == 1 and rows[0]["http_retry_decision"] == "deadline_after_backoff"
    assert rows[0]["http_retry_backoff_completed"] is True


async def test_cancelled_backoff_is_not_reported_as_slept_or_retried(monkeypatch):
    clock = RetryClock()
    clock.sleep_error = asyncio.CancelledError()
    client, requests, rows, _streams, _clock = http_retry_client(
        monkeypatch, [(502, [], 0)], clock=clock, transient_http_retries=2,
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await client.chat(MESSAGES)
    finally:
        await client.aclose()
    assert len(requests) == 1 and rows[0]["http_retry_decision"] == "backoff_interrupted"
    assert rows[0]["http_retry_backoff_completed"] is False
    assert rows[0]["http_retry_backoff_elapsed_s"] == 0
    assert rows[0]["http_retry_backoff_error_class"] == "CancelledError"


async def test_shared_deadline_does_not_turn_a_200_body_timeout_into_retry():
    gate = asyncio.Event()
    client, stream, requests, rows = client_for(
        [tool(0, identity="a", name="probe", args='{"partial":'), gate],
        timeout_s=0.05, transient_http_retries=2,
    )
    try:
        with pytest.raises(SchoolStreamTimeout):
            await client.chat(MESSAGES, TOOLS)
    finally:
        await client.aclose()
    assert len(requests) == 1 and stream.closed
    assert rows[0]["status"] == "failed" and rows[0]["partial_discarded"] is True
    assert rows[0]["error_class"] == "SchoolStreamTimeout"
    assert rows[0]["http_retry_backoff_planned_s"] is None


async def test_outer_timeout_cancellation_during_backoff_is_classified_by_context(monkeypatch):
    contexts = 0
    real_timeout, real_sleep = asyncio.timeout, asyncio.sleep

    def short_outer_timeout(seconds):
        nonlocal contexts
        contexts += 1
        # Exercise the real outer cancellation branch without waiting 180 seconds.
        return real_timeout(0.2 if contexts == 1 else seconds)

    async def waiting_backoff(seconds):
        assert seconds == 5
        await real_sleep(1)

    monkeypatch.setattr(stream_module, "asyncio", SimpleNamespace(
        timeout=short_outer_timeout, sleep=waiting_backoff, CancelledError=asyncio.CancelledError,
    ))
    requests, rows = [], []

    def respond(request):
        requests.append(request)
        return httpx.Response(502, stream=Chunks([]))

    client = SchoolStreamClient(
        api_key="offline", base_url="https://school.invalid", transient_http_retries=2,
        transport=httpx.MockTransport(respond), on_attempt=rows.append,
    )
    try:
        with pytest.raises(SchoolStreamTimeout):
            await client.chat(MESSAGES)
    finally:
        await client.aclose()
    assert len(requests) == 1 and rows[0]["call_deadline_expired"] is True
    assert rows[0]["http_retry_backoff_completed"] is False
    assert rows[0]["http_retry_backoff_error_class"] == "SchoolStreamTimeout"


async def test_close_failure_after_gateway_error_is_not_retryable(monkeypatch):
    clock = RetryClock()
    requests, rows = [], []
    monkeypatch.setattr(stream_module, "time", SimpleNamespace(monotonic=clock.now))
    monkeypatch.setattr(stream_module, "asyncio", SimpleNamespace(
        timeout=asyncio.timeout, sleep=clock.sleep, CancelledError=asyncio.CancelledError,
    ))

    class BrokenClose(Chunks):
        async def aclose(self):
            raise RuntimeError("local close failure")

    def respond(request):
        requests.append(request)
        return httpx.Response(502, stream=BrokenClose([]))

    client = SchoolStreamClient(
        api_key="offline", base_url="https://school.invalid", transient_http_retries=2,
        transport=httpx.MockTransport(respond), on_attempt=rows.append,
    )
    try:
        with pytest.raises(RuntimeError, match="local close failure"):
            await client.chat(MESSAGES)
    finally:
        await client.aclose()
    assert len(requests) == 1 and clock.sleeps == []
    assert rows[0]["error_class"] == "RuntimeError"
    assert rows[0]["http_retry_backoff_planned_s"] is None


async def test_user_cancel_during_200_partial_body_stays_cancelled():
    gate = asyncio.Event()
    client, stream, requests, rows = client_for(
        [tool(0, identity="a", name="probe", args='{"partial":'), gate],
        transient_http_retries=2,
    )
    job = asyncio.create_task(client.chat(MESSAGES, TOOLS))
    try:
        await asyncio.wait_for(stream.started.wait(), 1)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
    finally:
        await client.aclose()
    assert len(requests) == 1 and stream.closed
    assert rows[0]["status"] == "cancelled" and rows[0]["partial_discarded"] is True
    assert rows[0]["call_deadline_expired"] is False
    assert rows[0]["http_retry_backoff_planned_s"] is None


@pytest.mark.parametrize("retries", [0, 2])
async def test_first_success_preserves_known_usage_and_missing_usage_stays_unknown(
    monkeypatch, retries,
):
    client, requests, rows, _streams, clock = http_retry_client(
        monkeypatch, [(200, [text_response("one", {
            "prompt_tokens": 4, "completion_tokens": 2,
        })], 0), (200, [text_response("two")], 0)], transient_http_retries=retries,
    )
    try:
        known = await client.chat(MESSAGES)
        unknown = await client.chat(MESSAGES)
    finally:
        await client.aclose()
    assert known.usage_present is True and known.tokens == 6
    assert unknown.usage_present is False and unknown.usage_raw == {}
    assert len(requests) == 2 and clock.sleeps == []
    assert [row["attempt"] for row in rows] == [1, 1]
    assert rows[0]["returned_call_usage_complete"] is True
    assert rows[1]["returned_call_usage_complete"] is False


async def test_concurrent_http_retries_keep_call_local_counters_and_payloads(monkeypatch):
    clock = RetryClock()
    real_sleep = asyncio.sleep
    monkeypatch.setattr(stream_module, "time", SimpleNamespace(monotonic=clock.now))

    async def backoff(seconds):
        clock.sleeps.append(seconds)
        clock.value += seconds
        await real_sleep(0)

    monkeypatch.setattr(stream_module, "asyncio", SimpleNamespace(
        timeout=asyncio.timeout, sleep=backoff, CancelledError=asyncio.CancelledError,
    ))
    by_name, rows, bodies = {}, [], []

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        name = body["tools"][0]["function"]["name"]
        by_name[name] = by_name.get(name, 0) + 1
        if by_name[name] == 1:
            return httpx.Response(502, stream=Chunks([]))
        wire = tool(0, identity=name, name=name, args="{}")
        wire += event(finish="tool_calls") + done()
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=Chunks([wire]))

    client = SchoolStreamClient(
        api_key="offline", base_url="https://school.invalid", transient_http_retries=2,
        tool_choice_policy="required-when-tools", transport=httpx.MockTransport(respond),
        on_attempt=rows.append,
    )
    try:
        results = await asyncio.gather(*(client.chat(MESSAGES, [{
            "type": "function", "function": {"name": name},
        }]) for name in ("alpha", "beta")))
    finally:
        await client.aclose()
    assert [response.tool_calls[0].name for response in results] == ["alpha", "beta"]
    assert by_name == {"alpha": 2, "beta": 2} and clock.sleeps == [5, 5]
    groups = [[row for row in rows if row["call_id"] == key]
              for key in {row["call_id"] for row in rows}]
    assert len(groups) == 2
    assert all([row["attempt"] for row in group] == [1, 2] for group in groups)
    assert all(body["tool_choice"] == "required" for body in bodies)


async def test_recording_llm_keeps_success_after_http_failure_billing_unknown(
    tmp_path, monkeypatch,
):
    client, _requests, rows, _streams, _clock = http_retry_client(
        monkeypatch, [(502, [], 0), (200, [text_response("complete", {
            "prompt_tokens": 9, "completion_tokens": 3,
        })], 0)], transient_http_retries=1,
    )
    recorder = RecordingLLM(client, tmp_path / "dialogue.jsonl")
    try:
        result = await recorder.chat(MESSAGES)
    finally:
        await recorder.aclose()
    summary = recorder.summary()["total"]
    assert result.content == "complete" and result.usage_present is False
    assert summary["calls"] == summary["usage_missing_calls"] == 1
    assert summary["tokens_in"] is summary["tokens_out"] is None
    assert rows[1]["usage_raw"] == {"prompt_tokens": 9, "completion_tokens": 3}


async def test_real_engine_502_then_complete_tool_executes_exactly_once(tmp_path, monkeypatch):
    calls = []

    class Probe:
        name = "probe"
        description = "Offline marker"
        action_class = "fs_write"
        timeout_s = 1
        params_schema = {"type": "object", "properties": {}}

        async def run(self, args, ctx):
            calls.append(args)
            Path(ctx.workspace, "marker.md").write_text("local-only", encoding="utf-8")
            return ToolResult(ok=True, artifacts=["marker.md"])

    first = tool(0, identity="a", name="probe", args="{}")
    first += event(finish="tool_calls") + done()
    final = tool(0, identity="b", name="submit_result", args=json.dumps({
        "summary": "Synthetic complete result", "files": ["marker.md"],
    })) + event(finish="tool_calls") + done()
    client, requests, rows, _streams, clock = http_retry_client(
        monkeypatch, [(502, [], 0), (200, [first], 0), (200, [final], 0)],
        transient_http_retries=2, tool_choice_policy="required-when-tools",
    )
    registry = ToolRegistry()
    registry.register_all(Probe(), SubmitResultTool())
    try:
        with Engine(client, tools=registry, settings=Settings(
                root=tmp_path / "run", memory_enabled=False,
                role_review_enabled=False)) as engine:
            root = engine.submit("Produce a synthetic marker", budget=Budget(
                max_retries=0, max_steps=3,
            ), acceptance=[Acceptance(kind="file_exists", target="marker.md")])
            await asyncio.wait_for(engine.run(), 3)
            assert engine.repo.get(root.id).status == "DONE"
    finally:
        await client.aclose()
    assert len(calls) == 1 and len(requests) == 3 and clock.sleeps == [5]
    assert requests[0] == requests[1] and all(b["tool_choice"] == "required" for b in requests)
    assert sorted(len([r for r in rows if r["call_id"] == key])
                  for key in {r["call_id"] for r in rows}) == [1, 2]


@pytest.mark.parametrize("mode", ["valid", "plain", "partial"])
async def test_real_engine_executes_only_a_complete_required_call(tmp_path, mode):
    calls, streams, requests, diagnostics = [], [], [], []

    class Probe:
        name = "probe"
        description = "Synthetic local marker; never executes for a rejected response."
        action_class = "fs_write"
        timeout_s = 1
        params_schema = {"type": "object", "properties": {}}

        async def run(self, args, ctx):
            calls.append(args)
            Path(ctx.workspace, "marker.md").write_text("local-only", encoding="utf-8")
            return ToolResult(ok=True, artifacts=["marker.md"])

    def respond(request):
        requests.append(json.loads(request.content))
        if mode == "plain":
            wire = text_response("a final-sounding response without a required call")
        elif len(requests) == 1:
            wire = tool(0, identity="a", name="probe", args="{}")
            wire += event(finish="tool_calls")
            if mode == "valid":
                wire += done()
        else:
            wire = tool(0, identity="b", name="submit_result", args=json.dumps({
                "summary": "Synthetic complete result", "files": ["marker.md"],
            })) + event(finish="tool_calls") + done()
        stream = Chunks([wire])
        streams.append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    client = SchoolStreamClient(api_key="offline", base_url="https://school.invalid",
                                tool_choice_policy="required-when-tools",
                                transport=httpx.MockTransport(respond),
                                on_attempt=diagnostics.append)
    registry = ToolRegistry()
    registry.register_all(Probe(), SubmitResultTool())
    try:
        with Engine(client, tools=registry, settings=Settings(
                root=tmp_path / "run", memory_enabled=False,
                role_review_enabled=False)) as engine:
            root = engine.submit("Produce a synthetic marker", budget=Budget(
                max_retries=0, max_steps=3,
            ), acceptance=[Acceptance(kind="file_exists", target="marker.md")])
            await asyncio.wait_for(engine.run(), 3)
            final = engine.repo.get(root.id)
            assert final.status == ("DONE" if mode == "valid" else "DEAD")
            assert len(calls) == (1 if mode == "valid" else 0)
            assert bool(final.artifacts) is (mode == "valid")
    finally:
        await client.aclose()
    assert len(requests) == (2 if mode == "valid" else 1)
    assert all(s.closed for s in streams)
    assert all(body["tool_choice"] == "required" for body in requests)
    assert bool(list((tmp_path / "run/workspaces").rglob("marker.md"))) is (mode == "valid")
    if mode == "plain":
        assert diagnostics[0]["required_tool_choice_satisfied"] is False
    elif mode == "partial":
        assert diagnostics[0]["required_tool_choice_satisfied"] is None
    else:
        assert all(row["required_tool_choice_satisfied"] is True for row in diagnostics)
