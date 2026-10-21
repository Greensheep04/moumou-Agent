"""Tool contract test — **owned by P0, enforced on P1**.

Run it against your implementations:

    pytest tests/test_tool_contract.py -q

Green here means the framework can drive your tools. If you add a tool, append
it to ``moumou.tools.builtin.default_tools()`` and this file picks it up
automatically — you should not need to edit the test.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from moumou.contracts import DEFAULT_APPROVAL, ToolContext, ToolResult
from moumou.tools import default_tools


@pytest.fixture(scope="module")
def tools() -> dict:
    return {t.name: t for t in default_tools()}


@pytest.fixture
def ctx(tmp_path) -> ToolContext:
    return ToolContext(agent_id="a@t1", task_id="t1", workspace=tmp_path)


def test_every_tool_declares_the_protocol(tools):
    assert tools, "no tools registered"
    for name, tool in tools.items():
        assert isinstance(tool.name, str) and tool.name == name
        assert isinstance(tool.description, str) and tool.description.strip()
        assert isinstance(tool.params_schema, dict)
        assert tool.action_class in DEFAULT_APPROVAL, (
            f"{name}: action_class {tool.action_class!r} is not in DEFAULT_APPROVAL, "
            "so the approval policy cannot classify it"
        )
        assert isinstance(tool.timeout_s, (int, float)) and tool.timeout_s > 0


def test_params_schema_is_function_calling_shaped(tools):
    for name, tool in tools.items():
        schema = tool.params_schema
        assert schema.get("type") == "object", f"{name}: parameters must be an object"
        assert isinstance(schema.get("properties", {}), dict), f"{name}: properties"
        required = schema.get("required", [])
        assert isinstance(required, list), f"{name}: required must be a list"
        for key in required:
            assert key in schema.get("properties", {}), (
                f"{name}: required key {key!r} is not declared in properties"
            )
        assert "$ref" not in str(schema), f"{name}: schema still has $ref; inline it first"


async def test_no_tool_raises_on_garbage_arguments(tools, ctx):
    garbage = [{}, {"path": 123}, {"command": None}, {"text": ["a"]}, {"nonsense": True}]
    for name, tool in tools.items():
        for args in garbage:
            try:
                result = await tool.run(args, ctx)
            except Exception as exc:                     # noqa: BLE001 - that's the point
                pytest.fail(f"{name} raised {type(exc).__name__} on {args}: {exc}")
            assert isinstance(result, ToolResult), f"{name} did not return a ToolResult"
            assert isinstance(result.ok, bool)


@pytest.mark.parametrize("escape", ["../outside.txt", "../../etc/passwd", "/etc/passwd"])
async def test_filesystem_tools_refuse_to_escape(tools, ctx, escape):
    for name in ("read_file", "write_file", "delete_file", "run_python"):
        result = await tools[name].run({"path": escape, "content": "x"}, ctx)
        assert isinstance(result, ToolResult)
        if escape.startswith("/") and name == "read_file":
            # absolute paths inside the workspace are fine; this one is outside
            assert not result.ok
        assert not result.ok, f"{name} accepted an escaping path: {escape}"


async def test_write_read_delete_roundtrip(tools, ctx):
    written = await tools["write_file"].run({"path": "a/b.txt", "content": "hello"}, ctx)
    assert written.ok and written.artifacts == ["a/b.txt"]

    read = await tools["read_file"].run({"path": "a/b.txt"}, ctx)
    assert read.ok and read.stdout == "hello"

    deleted = await tools["delete_file"].run({"path": "a/b.txt"}, ctx)
    assert deleted.ok
    assert not (ctx.workspace / "a" / "b.txt").exists()


async def test_reading_a_missing_file_is_an_error_not_a_crash(tools, ctx):
    result = await tools["read_file"].run({"path": "ghost.txt"}, ctx)
    assert not result.ok
    assert result.error and "ghost.txt" in result.error


async def test_shell_timeout_actually_kills_the_process(tools, ctx):
    started = time.monotonic()
    result = await tools["shell"].run(
        {"command": "python3 -c 'import time; time.sleep(30)'", "timeout_s": 0.5}, ctx
    )
    elapsed = time.monotonic() - started

    assert not result.ok
    assert result.error and "timeout" in result.error.lower()
    assert elapsed < 5, "the tool waited for the child process instead of killing it"


async def test_cancelling_a_tool_kills_its_child(tools, ctx):
    """Ctrl-C during `make eval` must not leave orphaned commands behind."""
    pid_file = ctx.workspace / "child.pid"
    script = (
        "import os, time; "
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
        "time.sleep(30)"
    )
    task = asyncio.create_task(
        tools["shell"].run({"command": f'python3 -c "{script}"', "timeout_s": 60}, ctx)
    )

    for _ in range(100):                      # wait for the child to announce itself
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.05)

    child_pid = int(pid_file.read_text().strip())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):   # signal 0 only checks existence
        os.kill(child_pid, 0)


async def test_large_output_is_truncated(tools, ctx):
    await tools["write_file"].run({"path": "big.txt", "content": "x" * 40_000}, ctx)
    result = await tools["read_file"].run({"path": "big.txt"}, ctx)
    assert result.ok
    assert result.truncated, "40KB read must be clipped or the model context explodes"
    assert len(result.stdout) < 20_000


def test_risky_tools_are_marked_for_approval(tools):
    assert tools["delete_file"].action_class == "fs_delete"
    assert tools["shell"].action_class == "shell"
    assert DEFAULT_APPROVAL["fs_delete"] is True
    assert DEFAULT_APPROVAL["shell"] is True
    assert DEFAULT_APPROVAL["fs_read"] is False
