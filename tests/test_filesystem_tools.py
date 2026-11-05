"""P1 filesystem behavior: boundaries, complete operations and bounded output."""

import json

import pytest

from moumou.contracts import ToolContext
from moumou.tools import default_tools


@pytest.fixture
def tools():
    return {t.name: t for t in default_tools()}


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(agent_id="a", task_id="t", workspace=tmp_path)


async def test_escape_has_stable_error_and_other_agent_is_unchanged(tools, ctx):
    sibling = ctx.workspace.parent / "other-agent.txt"
    sibling.write_text("private", encoding="utf-8")
    result = await tools["write_file"].run({"path": "../other-agent.txt", "content": "bad"}, ctx)
    assert not result.ok and result.error == "path_outside_workspace"
    assert sibling.read_text(encoding="utf-8") == "private"


async def test_append_list_search_and_delete_directory(tools, ctx):
    await tools["write_file"].run({"path": "sub/a.txt", "content": "hello"}, ctx)
    appended = await tools["write_file"].run(
        {"path": "sub/a.txt", "content": "\nworld", "mode": "append"}, ctx
    )
    assert appended.ok and appended.artifacts == ["sub/a.txt"]
    assert (ctx.workspace / "sub/a.txt").read_text(encoding="utf-8") == "hello\nworld"
    assert "list_files" in tools and "search_files" in tools
    listed = await tools["list_files"].run({"path": "sub"}, ctx)
    assert json.loads(listed.stdout) == ["sub/a.txt"]
    found = await tools["search_files"].run({"path": ".", "query": "world"}, ctx)
    assert json.loads(found.stdout) == [{"path": "sub/a.txt", "line": 2, "text": "world"}]
    deleted = await tools["delete_file"].run({"path": "sub", "recursive": True}, ctx)
    assert deleted.ok and "sub/a.txt" in deleted.artifacts
    assert not (ctx.workspace / "sub").exists()


async def test_patch_requires_unique_old_text(tools, ctx):
    assert "apply_patch" in tools
    (ctx.workspace / "a.txt").write_text("old old", encoding="utf-8")
    ambiguous = await tools["apply_patch"].run({"path": "a.txt", "old": "old", "new": "new"}, ctx)
    assert not ambiguous.ok
    assert (ctx.workspace / "a.txt").read_text(encoding="utf-8") == "old old"
    patched = await tools["apply_patch"].run({"path": "a.txt", "old": "old old", "new": "new"}, ctx)
    assert patched.ok and patched.artifacts == ["a.txt"]


async def test_unicode_output_is_limited_in_bytes(tools, ctx):
    (ctx.workspace / "a.txt").write_text("中" * 9000, encoding="utf-8")
    result = await tools["read_file"].run({"path": "a.txt"}, ctx)
    assert result.ok and result.truncated
    assert len(result.stdout.encode("utf-8")) <= 8192 + len("\n[truncated]")
    assert "\ufffd" not in result.stdout


@pytest.mark.parametrize("path", [".", ".git", ".git/config"])
async def test_protected_paths_cannot_be_deleted(tools, ctx, path):
    (ctx.workspace / ".git").mkdir(exist_ok=True)
    (ctx.workspace / ".git/config").write_text("evidence", encoding="utf-8")
    result = await tools["delete_file"].run({"path": path, "recursive": True}, ctx)
    assert not result.ok
    assert (ctx.workspace / ".git/config").read_text() == "evidence"


async def test_link_to_other_workspace_is_rejected(tools, ctx, tmp_path):
    import os
    import subprocess

    outside = tmp_path.parent / (tmp_path.name + "-private")
    outside.mkdir()
    (outside / "secret.txt").write_text("private", encoding="utf-8")
    link = ctx.workspace / "link"
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        result = await tools["read_file"].run({"path": "link/secret.txt"}, ctx)
        assert not result.ok and result.error == "path_outside_workspace"
    finally:
        # Remove the link itself; never recursively remove its target.
        if os.name == "nt":
            os.rmdir(link)
        else:
            link.unlink()


@pytest.mark.parametrize("path", [".git./config", ".git /config", "nested./out.txt",
                                  "nested /out.txt", ".GiT.../config"])
@pytest.mark.parametrize("existing_git", [False, True])
def test_jail_rejects_windows_trailing_alias_components(tmp_path, path, existing_git):
    from moumou.sandbox.local import LocalJail
    if existing_git:
        (tmp_path / ".git").mkdir()
    with pytest.raises((ValueError, PermissionError)):
        LocalJail(tmp_path).resolve(path, mutable=True)
