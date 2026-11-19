"""Offline contracts for explicit output limits and partial-response rejection."""

from __future__ import annotations

import asyncio
import builtins
import json
from pathlib import Path

import pytest
from test_quality_tools_cli import exercise, forbid, read, single_case
from test_school_stream import MESSAGES, TOOLS, client_for, done, event, tool

from moumou.config import LLMSettings
from moumou.contracts import Budget, ToolResult
from moumou.eval import cli
from moumou.eval import team as team_module
from moumou.eval.school_stream import SchoolStreamError
from moumou.orchestrator import Engine, Settings
from moumou.tools import ToolRegistry


@pytest.mark.parametrize("options", [
    ["--llm-completion-cap", "16384"],
    ["--llm-max-content-chars", "32768"],
    ["--llm-transport", "stream", "--llm-completion-cap", "0"],
    ["--llm-transport", "stream", "--llm-completion-cap", "-1"],
    ["--llm-transport", "stream", "--llm-max-content-chars", "0"],
    ["--llm-transport", "stream", "--llm-max-content-chars", "-1"],
    ["--llm-transport", "stream", "--llm-completion-cap", str(2**64)],
    ["--llm-transport", "stream", "--llm-max-content-chars", str(2**64)],
])
def test_invalid_cli_limits_stop_before_environment_or_session(tmp_path, monkeypatch, options):
    monkeypatch.setattr(LLMSettings, "from_env", classmethod(forbid))
    monkeypatch.setattr("moumou.config.load_dotenv", forbid)
    output = tmp_path / "must-not-exist"
    with pytest.raises(SystemExit) as error:
        cli.main([*options, "--out", str(output)])
    assert error.value.code == 2
    assert not output.exists()


@pytest.mark.parametrize("name,value", [
    ("llm_completion_cap", True), ("llm_completion_cap", 1.5),
    ("llm_completion_cap", "16384"), ("llm_completion_cap", 0),
    ("llm_max_content_chars", False), ("llm_max_content_chars", "32768"),
    ("llm_max_content_chars", -1), ("llm_max_content_chars", 2**4096),
])
async def test_programmatic_limits_refuse_before_keys_or_output(
    tmp_path, monkeypatch, name, value,
):
    bank, case, _roles, _sha = single_case(tmp_path)
    output = tmp_path / "must-not-exist"
    args = cli.parse_args(["--tasks", str(bank), "--out", str(output),
                           "--llm-transport", "stream"])
    setattr(args, name, value)
    monkeypatch.setattr(LLMSettings, "from_env", classmethod(forbid))
    with pytest.raises(ValueError):
        await cli.run_suite(args, [case])
    assert not output.exists()


def test_explicit_limit_plan_is_call_free_and_records_requests(
    tmp_path, monkeypatch, capsys,
):
    bank, _case, _roles, _sha = single_case(tmp_path)
    output = tmp_path / "plan-only-no-directory"
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
                     "--llm-transport", "stream", "--llm-completion-cap", "16384",
                     "--llm-max-content-chars", "32768",
                     "--require-declared-inputs"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["llm_completion_cap"] == 16384
    assert plan["llm_max_content_chars"] == 32768
    assert plan["llm_transport"] == "stream"
    assert plan["require_declared_inputs"] is True
    assert not output.exists()


async def test_formal_cli_forwards_limits_and_records_same_values(tmp_path, monkeypatch):
    original_parse = cli.parse_args

    def with_limits(options):
        return original_parse([*options, "--llm-completion-cap", "16384",
                               "--llm-max-content-chars", "32768",
                               "--require-declared-inputs"])

    monkeypatch.setattr(cli, "parse_args", with_limits)
    original_configure = team_module.configure_team
    observed = []

    def configured(*args, **kwargs):
        state = original_configure(*args, **kwargs)
        schema = args[0].tools.get("submit_plan").params_schema
        observed.append("inputs" in schema["properties"]["subtasks"]["items"]["required"])
        return state

    monkeypatch.setattr(team_module, "configure_team", configured)
    # This existing harness runs the real CLI/Engine with deterministic clients,
    # forbids network/key loading, and exercises actual source/file evidence tools.
    output, entry, constructors, attempts = await exercise(
        tmp_path, monkeypatch, transport="stream",
    )
    assert observed == [True]
    assert read(output / "test.json")["input_handoff_policy"]["required"] is True
    assert constructors[0]["completion_cap"] == 16384
    assert constructors[0]["max_content_chars"] == 32768
    model = read(output / "test.json")["model"]
    sidecar = read(output / entry["scenario_id"] / "stream-transport.json")
    for record in (model, sidecar):
        assert record["completion_cap"] == 16384
        assert record["max_content_chars"] == 32768
        assert record["provider_output_limit_compliance"] is None
    # The synthetic stream's first missing usage remains unknown in the real report.
    assert attempts[0]["usage_present"] is False
    assert entry["llm"]["total"]["tokens_in"] is None


def test_unselected_options_preserve_existing_constructor_policy():
    default = cli.parse_args([])
    selected = cli.parse_args(["--llm-transport", "stream"])
    assert default.llm_transport == "buffered"
    assert cli._stream_output_options(default, "buffered") == {}
    assert cli._stream_output_options(selected, "stream") == {}


async def test_fragmented_plain_overrun_discards_partial_tool_before_finish():
    chunks = [tool(0, identity="partial", name="probe", args='{"x":'),
              event({"content": "abc"}), event({"content": "猫🙂defg"}),
              tool(0, args='"never-executable"}'), event(finish="tool_calls"), done()]
    wire = b"".join(chunks)
    client, stream, requests, diagnostics = client_for(
        [wire[index:index + 1] for index in range(len(wire))],
        max_content_chars=8, completion_cap=16384,
        max_attempts=2, retry_transport=True,
    )
    try:
        with pytest.raises(SchoolStreamError, match="content bound"):
            await client.chat(MESSAGES, TOOLS)
    finally:
        await client.aclose()
    assert len(requests) == 1 and stream.closed
    record = diagnostics[0]
    assert record["status"] == "failed" and record["partial_discarded"]
    assert record["requested_completion_cap"] == 16384
    assert record["max_content_chars"] == 8
    assert record["max_argument_chars"] == 256 * 1024
    assert record["content_chars"] == 3 and record["content_chars_received"] == 9
    assert record["content_bound_exceeded"]
    assert record["limit_triggered"] == "ordinary_content_characters"
    assert record["first_content_s"] is not None
    assert record["done"] is False and record["finish_reason"] is None
    assert record["usage_present"] is False and record["usage_raw"] == {}
    assert record["provider_output_limit_compliance"] is None
    saved = json.dumps(record)
    for private in ("private-request-text", "never-executable", "offline-key", "猫"):
        assert private not in saved


async def test_long_markdown_arguments_have_a_separate_bound_from_plain_text():
    document = "# A long local document\n\n" + "A complete paragraph.\n" * 200
    arguments = json.dumps({"content": document})
    chunks = [event({"content": "OK"}),
              tool(0, identity="a", name="probe", args=arguments[:100]),
              tool(0, args=arguments[100:]), event(finish="tool_calls"), done()]
    client, stream, requests, diagnostics = client_for(
        chunks, max_content_chars=2, completion_cap=16384,
    )
    try:
        response = await client.chat(MESSAGES, TOOLS)
    finally:
        await client.aclose()
    assert response.tool_calls[0].args["content"] == document
    assert response.content == "OK" and stream.closed
    assert json.loads(requests[0].content)["max_tokens"] == 16384
    assert response.usage_present is False
    assert diagnostics[0]["limit_triggered"] is None
    assert diagnostics[0]["content_bound_exceeded"] is False
    assert diagnostics[0]["provider_output_limit_compliance"] is None


@pytest.mark.parametrize("failure", ["ordinary_content", "provider_length"])
async def test_real_engine_never_executes_partial_tool_or_accepts_length_finish(
    tmp_path, failure,
):
    calls = []

    class Probe:
        name = "probe"
        description = "Offline test side effect; must never be reached."
        action_class = "fs_read"
        timeout_s = 1
        params_schema = {"type": "object", "properties": {"x": {"type": "integer"}}}

        async def run(self, args, ctx):
            calls.append(args)
            Path(ctx.workspace, "should-not-exist.md").write_text("partial executed")
            return ToolResult(ok=True)

    prefix = tool(0, identity="a", name="probe", args='{"x":')
    suffix = tool(0, args="1}")
    if failure == "ordinary_content":
        chunks = [prefix, event({"content": "too much ordinary text"}), suffix,
                  event(finish="tool_calls"), done()]
    else:
        chunks = [prefix, suffix, event(finish="length"), done()]
    client, stream, requests, diagnostics = client_for(chunks, max_content_chars=2)
    registry = ToolRegistry()
    registry.register(Probe())
    try:
        with Engine(client, tools=registry, settings=Settings(
                root=tmp_path / "engine", memory_enabled=False,
                role_review_enabled=False)) as engine:
            root = engine.submit("Exercise a deterministic failed stream",
                                 budget=Budget(max_retries=0, max_steps=3))
            await asyncio.wait_for(engine.run(), 3)
            final = engine.repo.get(root.id)
            assert final.status == "DEAD"
            assert "SchoolStreamError" in final.error
            assert not final.result and not final.artifacts
    finally:
        await client.aclose()
    assert calls == [] and len(requests) == 1 and stream.closed
    assert not list((tmp_path / "engine/workspaces").rglob("should-not-exist.md"))
    expected = ("ordinary_content_characters" if failure == "ordinary_content"
                else "provider_length_finish")
    assert diagnostics[0]["limit_triggered"] == expected
    assert diagnostics[0]["partial_discarded"] is True
    assert diagnostics[0]["usage_present"] is False


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
async def test_invalid_programmatic_input_mode_refuses_before_keys_or_output(
    tmp_path, monkeypatch, value,
):
    bank, case, _roles, _sha = single_case(tmp_path)
    output = tmp_path / "invalid-input-mode"
    args = cli.parse_args(["--tasks", str(bank), "--out", str(output)])
    args.require_declared_inputs = value
    monkeypatch.setattr(LLMSettings, "from_env", classmethod(forbid))
    with pytest.raises(ValueError):
        await cli.run_suite(args, [case])
    assert not output.exists()
