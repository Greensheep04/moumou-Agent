"""Live validation stays opt-in and records Gate decisions without credentials."""

import json
from pathlib import Path

import httpx
import pytest

from moumou.demo import p1_live
from moumou.tools.search import WebSearchTool
from moumou.workspace.git import run_git


def repository(path):
    path.mkdir()
    run_git(path, "init", "--initial-branch=main")
    (path / "hello.txt").write_text("hello", encoding="utf-8")
    run_git(path, "add", "hello.txt")
    run_git(path, "commit", "-m", "initial content")
    return path


def test_push_cli_requires_explicit_execution_and_remote():
    with pytest.raises(SystemExit):
        p1_live.parse_args(["--check", "push"])
    with pytest.raises(SystemExit):
        p1_live.parse_args(["--check", "push", "--remote-url", "https://example.org/repo.git"])
    args = p1_live.parse_args(["--check", "push", "--execute-push", "--remote-url", "remote.git"])
    assert args.execute_push


async def test_search_missing_key_has_no_network(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    result = await p1_live.validate_search(tmp_path / "check", "multi-agent systems")
    assert not result["ok"] and result["error"] == "TAVILY_API_KEY is not configured"
    assert (tmp_path / "check/summary.json").is_file()


async def test_search_loads_local_env_and_records_only_metadata(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    (tmp_path / ".env").write_text("TAVILY_API_KEY=test-only-secret\n", encoding="utf-8")
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer test-only-secret"
        return httpx.Response(200, json={"results": [
            {"title": "Example", "url": "https://example.org", "content": "A short result"}
        ]})

    monkeypatch.setattr(p1_live, "default_tools", lambda: [
        WebSearchTool(transport=httpx.MockTransport(respond))
    ])
    result = await p1_live.validate_search(tmp_path / "check", "multi-agent systems")
    assert result["ok"] and result["result_count"] == 1 and result["fields_valid"]
    assert len(requests) == 1
    for path in (tmp_path / "check").rglob("*.json*"):
        assert "test-only-secret" not in path.read_text(encoding="utf-8")
    assert "short result" not in json.dumps(result)


async def test_push_wrapper_checks_real_refs_and_gate_decisions(tmp_path):
    source = repository(tmp_path / "source")
    remote = tmp_path / "remote.git"
    run_git(tmp_path, "init", "--bare", str(remote))
    run_git(source, "remote", "add", "baseline", str(remote))
    run_git(source, "push", "baseline", "HEAD:refs/heads/main")
    head = run_git(source, "rev-parse", "HEAD").stdout.strip()
    summary = await p1_live.validate_push(tmp_path / "check", source, str(remote))
    assert summary["ok"] and summary["approved_ref"] == head
    assert summary["rejected_ref"] is None
    assert summary["rejected_error"] == "rejected_by_human"
    assert summary["approval_mode"] == "scripted"
    assert run_git(remote, "rev-parse", "refs/heads/main").stdout.strip() == head
    assert run_git(source, "branch", "--format=%(refname:short)").stdout.strip() == "main"
    spans = [json.loads(line) for line in Path(summary["trace"]).read_text(
        encoding="utf-8"
    ).splitlines()]
    assert [e["payload"]["decision"] for e in spans if e["kind"] == "approval"] == [
        "approve", "reject"
    ]


async def test_push_rejects_credentials_in_remote_url(tmp_path):
    with pytest.raises(ValueError, match="credential"):
        await p1_live.validate_push(tmp_path / "check", tmp_path,
                                    "https://user:secret@example.org/repo.git")


def test_cli_reports_git_timeout_without_traceback(monkeypatch, capsys):
    from subprocess import TimeoutExpired

    async def timed_out(*args):
        raise TimeoutExpired(["git", "ls-remote"], 30)

    monkeypatch.setattr(p1_live, "validate_push", timed_out)
    assert p1_live.main(["--check", "push", "--execute-push", "--remote-url", "remote.git"]) == 1
    assert not json.loads(capsys.readouterr().out)["ok"]
