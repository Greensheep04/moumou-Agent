"""Real read_file clients can reconstruct bounded UTF-8 pages without shell reads."""

import json
import os
import subprocess

import pytest

from moumou.contracts import ToolContext
from moumou.tools.filesystem import ReadFileTool


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(agent_id="reader", task_id="paging", workspace=tmp_path)


@pytest.fixture
def tool():
    return ReadFileTool()


@pytest.mark.parametrize(
    ("payload", "expected", "truncated"),
    [
        (b"", "", False),
        (b"A" * 8192, "A" * 8192, False),
        (b"A" * 9000, "A" * 8192 + "\n[truncated]", True),
    ],
)
async def test_unpaged_preview_keeps_existing_output_contract(
    ctx, tool, payload, expected, truncated,
):
    (ctx.workspace / "preview.txt").write_bytes(payload)
    result = await tool.run({"path": "preview.txt"}, ctx)
    assert result.ok and result.stdout == expected
    assert result.truncated is truncated and result.stderr == ""
    assert result.artifacts == [] and tool.action_class == "fs_read"


async def collect_pages(ctx, tool, filename, payload, limit):
    path = ctx.workspace / filename
    path.write_bytes(payload)
    cursor, chunks, seen = 0, [], set()
    for _ in range(len(payload) + 1):
        assert cursor not in seen, "pagination cursor repeated"
        seen.add(cursor)
        result = await tool.run({"path": filename, "offset": cursor, "limit": limit}, ctx)
        assert result.ok, result.error
        data = result.stdout.encode("utf-8")
        meta = json.loads(result.stderr)
        assert meta["offset"] == cursor and meta["limit"] == limit
        assert meta["bytes_returned"] == len(data) <= limit
        assert meta["next_offset"] == cursor + len(data)
        assert meta["file_size"] == len(payload)
        assert isinstance(meta["eof"], bool) and result.truncated is not meta["eof"]
        assert result.artifacts == []
        chunks.append(data)
        if meta["eof"]:
            assert meta["next_offset"] == len(payload)
            break
        assert cursor < meta["next_offset"] < len(payload)
        cursor = meta["next_offset"]
    else:
        pytest.fail("pagination did not reach EOF")
    assert b"".join(chunks) == payload
    assert path.read_bytes() == payload
    return chunks


async def test_long_single_line_json_is_complete_without_a_shell(ctx, tool):
    expected = {"report": "数据与评测🔍" * 4000, "sequence": list(range(1200))}
    payload = json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert b"\n" not in payload and len(payload) > 8192 * 4
    chunks = await collect_pages(ctx, tool, "one-line.json", payload, 8192)
    assert len(chunks) > 4 and json.loads(b"".join(chunks)) == expected


@pytest.mark.parametrize("limit", [4, 5, 7, 8191, 8192])
async def test_unicode_and_crlf_round_trip_without_lost_or_repeated_bytes(ctx, tool, limit):
    payload = ("X中😀é\r\n" * 30).encode("utf-8")
    await collect_pages(ctx, tool, "mixed.txt", payload, limit)


@pytest.mark.parametrize("prefix", [8190, 8191])
async def test_default_page_cursor_stops_before_a_partial_multibyte_character(ctx, tool, prefix):
    payload = ("A" * prefix + "😀中文tail").encode("utf-8")
    (ctx.workspace / "boundary.txt").write_bytes(payload)
    first = await tool.run({"path": "boundary.txt", "offset": 0}, ctx)
    assert first.ok and first.stdout == "A" * prefix
    assert json.loads(first.stderr)["next_offset"] == prefix
    rest = await tool.run({"path": "boundary.txt", "offset": prefix}, ctx)
    assert rest.ok and rest.stdout == "😀中文tail"
    assert (first.stdout + rest.stdout).encode("utf-8") == payload


@pytest.mark.parametrize(
    ("args", "expected", "limit", "next_offset", "eof"),
    [
        ({"offset": 0}, "abcdefghij", 8192, 10, True),
        ({"limit": 5}, "abcde", 5, 5, False),
    ],
)
async def test_explicit_paging_parameters_enable_documented_defaults(
    ctx, tool, args, expected, limit, next_offset, eof,
):
    (ctx.workspace / "defaults.txt").write_bytes(b"abcdefghij")
    result = await tool.run({"path": "defaults.txt", **args}, ctx)
    assert result.ok and result.stdout == expected
    assert json.loads(result.stderr) == {
        "offset": 0, "next_offset": next_offset, "limit": limit,
        "bytes_returned": len(expected), "file_size": 10, "eof": eof,
    }


@pytest.mark.parametrize("text", ["", "中文😀"])
async def test_offset_at_eof_returns_an_empty_terminal_page(ctx, tool, text):
    payload = text.encode("utf-8")
    (ctx.workspace / "eof.txt").write_bytes(payload)
    result = await tool.run({"path": "eof.txt", "offset": len(payload)}, ctx)
    assert result.ok and result.stdout == "" and not result.truncated
    meta = json.loads(result.stderr)
    assert meta["eof"] is True and meta["next_offset"] == len(payload)
    assert meta["bytes_returned"] == 0


@pytest.mark.parametrize("value", [True, False, None, -1, 1.0, "0", 5, 2**80])
async def test_invalid_byte_offsets_are_rejected_without_output(ctx, tool, value):
    (ctx.workspace / "small.txt").write_bytes(b"abcd")
    result = await tool.run({"path": "small.txt", "offset": value}, ctx)
    assert not result.ok and "offset" in result.error
    assert result.stdout == "" and (ctx.workspace / "small.txt").read_bytes() == b"abcd"


@pytest.mark.parametrize("value", [True, False, None, 0, -1, 8193, 1.0, "4", 2**80])
async def test_invalid_page_limits_are_rejected_without_output(ctx, tool, value):
    (ctx.workspace / "small.txt").write_bytes(b"abcd")
    result = await tool.run({"path": "small.txt", "limit": value}, ctx)
    assert not result.ok and "limit" in result.error and result.stdout == ""


@pytest.mark.parametrize("offset", [1, 2, 4, 5, 6])
async def test_cursor_inside_a_utf8_character_is_rejected(ctx, tool, offset):
    (ctx.workspace / "unicode.txt").write_bytes("中😀Z".encode())
    result = await tool.run({"path": "unicode.txt", "offset": offset}, ctx)
    assert not result.ok and "UTF-8 character boundary" in result.error
    assert result.stdout == ""


@pytest.mark.parametrize(("text", "limit"), [("中", 1), ("中", 2), ("😀", 3)])
async def test_too_small_budget_errors_instead_of_returning_a_stalled_cursor(
    ctx, tool, text, limit,
):
    (ctx.workspace / "unicode.txt").write_bytes(text.encode("utf-8"))
    result = await tool.run({"path": "unicode.txt", "offset": 0, "limit": limit}, ctx)
    assert not result.ok and "limit is too small" in result.error and result.stdout == ""
    retry = await tool.run({"path": "unicode.txt", "offset": 0, "limit": 4}, ctx)
    assert retry.ok and retry.stdout == text and json.loads(retry.stderr)["eof"] is True


@pytest.mark.parametrize("payload", [b"ok\xffbad", b"ok\xe4", b"ok\xe4A"])
async def test_invalid_utf8_is_reported_instead_of_silently_dropped(ctx, tool, payload):
    (ctx.workspace / "invalid.txt").write_bytes(payload)
    result = await tool.run({"path": "invalid.txt", "offset": 0}, ctx)
    assert not result.ok and "UTF-8" in result.error and result.stdout == ""


@pytest.mark.parametrize(
    "path", ["../other-reader.txt", ".git/config", ".git./config", ".GiT/config"],
)
async def test_paging_keeps_workspace_and_git_metadata_boundaries(ctx, tool, path):
    other = ctx.workspace.parent / "other-reader.txt"
    other.write_bytes(b"private sibling")
    (ctx.workspace / ".git").mkdir()
    (ctx.workspace / ".git/config").write_bytes(b"private metadata")
    result = await tool.run({"path": path, "offset": 0, "limit": 8192}, ctx)
    assert not result.ok and result.stdout == ""
    assert other.read_bytes() == b"private sibling"
    assert (ctx.workspace / ".git/config").read_bytes() == b"private metadata"


async def test_owner_private_context_remains_readable_under_existing_jail_policy(ctx, tool):
    context_dir = ctx.workspace / ".evaluation/context"
    context_dir.mkdir(parents=True)
    content = '{"goal":"当前角色自身的上下文"}'
    (context_dir / "owner.json").write_bytes(content.encode("utf-8"))
    result = await tool.run({"path": ".evaluation/context/owner.json", "offset": 0}, ctx)
    assert result.ok and result.stdout == content and json.loads(result.stderr)["eof"] is True


async def test_paging_does_not_follow_a_link_into_another_private_workspace(ctx, tool):
    outside = ctx.workspace.parent / (ctx.workspace.name + "-other-role")
    outside.mkdir()
    (outside / "source.txt").write_bytes(b"another role's source")
    link = ctx.workspace / "jump"
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            check=True, capture_output=True,
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        result = await tool.run({"path": "jump/source.txt", "offset": 0}, ctx)
        assert not result.ok and result.error == "path_outside_workspace"
        assert result.stdout == ""
        assert (outside / "source.txt").read_bytes() == b"another role's source"
    finally:
        if os.name == "nt":
            os.rmdir(link)
        else:
            link.unlink()


def test_schema_describes_bounded_byte_cursors_without_changing_approval_class(tool):
    schema = tool.params_schema
    assert schema["required"] == ["path"]
    assert schema["properties"]["offset"]["type"] == "integer"
    assert schema["properties"]["offset"]["minimum"] == 0
    assert schema["properties"]["limit"]["maximum"] == 8192
    assert "next_offset" in tool.description and "eof=true" in tool.description
    assert tool.action_class == "fs_read"
