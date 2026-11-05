"""Opt-in service checks through Gate; no model requests are made."""

import argparse
import asyncio
import json
import uuid
from pathlib import Path
from subprocess import TimeoutExpired
from urllib.parse import urlsplit

from moumou.adapters import FakeLLM, ScriptedHuman
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.config import load_dotenv
from moumou.contracts import AgentInstance
from moumou.orchestrator import Engine, Settings
from moumou.tools import ToolRegistry, default_tools
from moumou.workspace.git import run_git


def _engine(root, human):
    registry = ToolRegistry()
    registry.register_all(*default_tools())
    return Engine(
        FakeLLM(lambda messages, tools: LLMResponse()), human=human, tools=registry,
        settings=Settings(root=root / "run", memory_enabled=False, role_review_enabled=False),
    )


async def _execute(engine, name, args, workspace):
    node = engine.submit(f"Validate {name}")
    engine.repo.transition(node.id, "RUNNING", "validation")
    agent = AgentInstance(
        id=f"validator@{node.id}", role=node.role, project_id=node.project_id,
        conversation_id=node.conversation_id, workspace=str(workspace),
    )
    result = await engine.gate.execute(
        ToolCall(id=node.id, name=name, args=args), engine.repo.get(node.id), agent, workspace
    )
    engine.repo.transition(node.id, "DONE" if result.ok else "DEAD", "validation_result")
    return result


def _save(root, summary):
    summary["trace"] = str(root / "run" / "trace.jsonl")
    (root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


async def validate_search(root: Path, query: str, max_results: int = 3) -> dict:
    load_dotenv()
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    with _engine(root, ScriptedHuman(default="reject")) as engine:
        workspace = engine.workspace.allocate("default", "default", "validator")
        result = await _execute(engine, "web_search", {
            "query": query, "max_results": max_results
        }, workspace)
    items = json.loads(result.stdout) if result.ok else []
    valid = isinstance(items, list) and len(items) <= max_results and all(
        isinstance(item, dict) and all(isinstance(item.get(key), str)
                                      for key in ("title", "url", "snippet"))
        for item in items
    )
    return _save(root, {
        "check": "search", "ok": result.ok and valid, "result_count": len(items),
        "fields_valid": result.ok and valid,
        "error": result.error if not result.ok else None if valid else "invalid search result",
    })


def _prepare_push(root, source, remote_url, approved, rejected):
    checkout = root / "repository"
    run_git(root, "clone", "--no-hardlinks", "--", str(Path(source).resolve()), str(checkout))
    run_git(checkout, "remote", "set-url", "origin", remote_url)
    for branch in (approved, rejected):
        if _remote_ref(checkout, branch) is not None:
            raise ValueError("validation branch already exists; use a new run")
        run_git(checkout, "branch", "--", branch)
    return checkout, run_git(checkout, "rev-parse", "HEAD").stdout.strip()


def _remote_ref(checkout, branch):
    output = run_git(checkout, "ls-remote", "--heads", "origin", f"refs/heads/{branch}").stdout
    return output.split()[0] if output.strip() else None


async def validate_push(root: Path, source: Path, remote_url: str) -> dict:
    parsed = urlsplit(remote_url)
    if parsed.scheme in ("http", "https") and (parsed.username or parsed.password):
        raise ValueError("remote URL must not contain credentials")
    if not remote_url or remote_url.startswith("-") or "\0" in remote_url:
        raise ValueError("invalid remote URL")
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    suffix = uuid.uuid4().hex[:12]
    approved, rejected = f"p1-verify/approved-{suffix}", f"p1-verify/rejected-{suffix}"
    checkout, head = await asyncio.to_thread(
        _prepare_push, root, source, remote_url, approved, rejected
    )
    human = ScriptedHuman(default="reject", on_ask=lambda req: (
        "approve" if req.action_class == "git_push" and req.payload.get("branch") == approved
        else "reject"
    ))
    with _engine(root, human) as engine:
        accepted = await _execute(engine, "git_push", {"branch": approved}, checkout)
        denied = await _execute(engine, "git_push", {"branch": rejected}, checkout)
    accepted_ref = await asyncio.to_thread(_remote_ref, checkout, approved)
    denied_ref = await asyncio.to_thread(_remote_ref, checkout, rejected)
    ok = (accepted.ok and accepted_ref == head and not denied.ok
          and denied.error == "rejected_by_human" and denied_ref is None)
    return _save(root, {
        "check": "push", "ok": ok, "source_commit": head, "approval_mode": "scripted",
        "approved_branch": approved, "approved_ref": accepted_ref,
        "rejected_branch": rejected, "rejected_ref": denied_ref, "rejected_error": denied.error,
        "error": None if ok else accepted.error or "remote state did not match Gate decisions",
    })


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", choices=("search", "push"), default="search")
    parser.add_argument("--root", default="runs/p1-live")
    parser.add_argument("--query", default="multi-agent software engineering")
    parser.add_argument("--max-results", type=int, choices=range(1, 6), default=3)
    parser.add_argument("--source", default=".", help="trusted local Git repository to clone")
    parser.add_argument("--remote-url", help="explicit test remote; never embed credentials")
    parser.add_argument("--execute-push", action="store_true",
                        help="allow a scripted approve/reject check on fresh validation branches")
    args = parser.parse_args(argv)
    if args.check == "push" and (not args.execute_push or not args.remote_url):
        parser.error("push validation requires --execute-push and --remote-url")
    return args


def main(argv=None):
    args = parse_args(argv)
    root = Path(args.root) / uuid.uuid4().hex[:12]
    try:
        if args.check == "push":
            summary = asyncio.run(validate_push(root, Path(args.source), args.remote_url))
        else:
            summary = asyncio.run(validate_search(root, args.query, args.max_results))
    except (OSError, ValueError, RuntimeError, TimeoutExpired) as exc:
        summary = {"ok": False, "error": f"{type(exc).__name__}: validation setup failed"}
    print(json.dumps(summary, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
