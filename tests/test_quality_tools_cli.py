"""Offline CLI contracts for quality tools, transport selection and delivery evidence.

Generic local source/artifact fixtures contain no teacher-task answers. Real
session execution uses deterministic clients and forbids external HTTP.
"""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import json
from pathlib import Path

import pytest

from moumou.adapters import FakeLLM
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.config import LLMSettings
from moumou.contracts import ToolContext, ToolResult
from moumou.eval import cli
from moumou.eval.cases import load_cases
from moumou.eval.source_evidence import SourceEvidenceStore

MEMBERS = (("coordinator", "Coordinator"), ("writer", "Writer"),
           ("checker", "Checker"), ("publisher", "Publisher"))
URL = "https://example.test/retained-fixture"
SOURCE = "Offline source: café is a literal fixture quotation."
ARTIFACT = "# Fixture delivery\n\nThe literal quotation is café.\n"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def single_case(tmp_path):
    root = tmp_path / "generic-cases"
    folder = root / "scenario_1 with spaces"
    folder.mkdir(parents=True)
    role_text = "# Scenario 1: Generic file workflow\n\n| Agent ID | Role |\n|---|---|\n"
    role_text += "\n".join(f"| `{name}` | {title} |" for name, title in MEMBERS) + "\n\n"
    for name, title in MEMBERS:
        role_text += (f"## {title} ({name})\n\n```text\n"
                      f"You are the fixture {name}; retain this responsibility."
                      "\n```\n\n")
    roles = folder / "01_role_initialization.md"
    roles.write_text(role_text, encoding="utf-8")
    (folder / "02_task_1.md").write_text(
        "# Task 1\n\n```text\nWrite report.md and verify its literal quotation.\n```\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        "<!-- evaluation-bank: supplementary -->\n"
        f"[Team]({folder.name}/01_role_initialization.md) | "
        f"[Task 1]({folder.name}/02_task_1.md)\n", encoding="utf-8",
    )
    return root, load_cases(root)[0], roles, hashlib.sha256(roles.read_bytes()).hexdigest()


def response(name, **arguments):
    return LLMResponse(tool_calls=[ToolCall(id=name, name=name, args=arguments)],
                       usage_present=True, tokens_in=17, tokens_out=5,
                       usage_raw={"prompt_tokens": 17, "completion_tokens": 5})


def forbid(*args, **kwargs):
    raise AssertionError("Offline integration attempted forbidden environment/network/default")


async def exercise(tmp_path, monkeypatch, *, transport, reasoning_effort=None):
    from moumou.eval import school_stream
    from moumou.tools import artifact_quote, source_audit

    bank, case, role_path, role_sha = single_case(tmp_path)
    out = tmp_path / "session"
    seeded, constructors, emitted, observed_tools = {}, [], [], []
    actual_store = SourceEvidenceStore

    def retained_store(directory):
        store = actual_store(directory)
        folder = Path(directory).parent
        ctx = ToolContext(agent_id=case.leader, task_id="t1", workspace=(
            folder / "workspaces" / case.id / "instructor-group" / case.leader
        ))
        store.record_search("Offline fixture", ToolResult(ok=True, stdout=json.dumps([
            {"url": URL, "title": "Offline fixture", "snippet": "A registered fixture."},
        ])), ctx, request_id="offline-fixture")
        metadata = store.record_document(URL, URL, SOURCE.encode(), SOURCE, "text/plain", "Fixture")
        store.record_read(URL, ctx, ok=True, metadata=metadata, offset=0, limit=len(SOURCE))
        seeded.update(metadata)
        return store

    class ObservedAudit(source_audit.SourceAccessAuditTool):
        async def run(self, args, ctx):
            result = await super().run(args, ctx)
            observed_tools.append((self.name, result))
            return result

    class ObservedQuote(artifact_quote.VerifyArtifactQuoteTool):
        async def run(self, args, ctx):
            result = await super().run(args, ctx)
            observed_tools.append((self.name, result))
            return result

    def script(messages, tools):
        names = {item.get("function", {}).get("name") for item in (tools or [])}
        if names == {"submit_lesson"}:
            return response("submit_lesson", lessons=[])
        assert {"verify_artifact_quote", "source_access_audit"} <= names
        assert "verify_artifact_quote" in messages[0]["content"]
        assert "source_access_audit" in messages[0]["content"]
        previous = messages[-1].get("tool_call_id") if messages[-1]["role"] == "tool" else None
        if previous is None:
            return response("write_file", path="report.md", content=ARTIFACT)
        if previous == "write_file":
            return response("source_access_audit", agent_id="*", all_tasks=True)
        if previous == "source_access_audit":
            start = ARTIFACT.index("café")
            return response("verify_artifact_quote", path="report.md", start=start, end=start + 4,
                            metadata_sha256=seeded["metadata_sha256"])
        assert previous == "verify_artifact_quote"
        return response("submit_result", summary="Offline delivered fixture", files=["report.md"])

    class ObservedStream(FakeLLM):
        def __init__(self, callback):
            super().__init__(script)
            self.callback = callback

        async def chat(self, messages, tools=None):
            result = await super().chat(messages, tools)
            measured = bool(emitted)  # First actual stub call deliberately has unknown usage.
            result = result.model_copy(update={
                "model": "offline-returned-model", "usage_present": measured,
                "tokens_in": 17 if measured else 0, "tokens_out": 5 if measured else 0,
                "usage_raw": {"prompt_tokens": 17, "completion_tokens": 5} if measured else {},
            })
            record = {"call_id": f"fixture-stream-{len(emitted) + 1}", "attempt": 1,
                      "status": "completed", "partial_discarded": False,
                      "configured_model": "offline-requested-model", "returned_model": result.model,
                      "HTTP_status": 200, "headers_s": 0.01, "first_body_byte_s": 0.02,
                      "completion_s": 0.03, "usage_present": measured,
                      "usage_raw": dict(result.usage_raw)}
            emitted.append(record.copy())
            self.callback(record)
            return result

    def buffered_factory(**kwargs):
        assert transport == "buffered"
        constructors.append(kwargs)
        return FakeLLM(script)

    def stream_factory(**kwargs):
        assert transport == "stream"
        constructors.append(kwargs)
        return ObservedStream(kwargs["on_attempt"])

    settings = LLMSettings(api_key="offline-controller-key", model="offline-requested-model",
                           base_url="https://offline.invalid", timeout_s=1)
    monkeypatch.setattr(LLMSettings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr("moumou.config.load_dotenv", forbid)
    monkeypatch.setattr("httpx.AsyncClient", forbid)
    monkeypatch.setattr("httpx.Client", forbid)
    monkeypatch.setattr("moumou.adapters.DeepSeekClient", buffered_factory)
    monkeypatch.setattr(school_stream, "SchoolStreamClient", stream_factory)
    monkeypatch.setattr("moumou.eval.source_evidence.SourceEvidenceStore", retained_store)
    monkeypatch.setattr(source_audit, "SourceAccessAuditTool", ObservedAudit)
    monkeypatch.setattr(artifact_quote, "VerifyArtifactQuoteTool", ObservedQuote)
    monkeypatch.setattr("moumou.eval.artifacts._code_version", lambda: {
        "git_commit": "a" * 40, "source_sha256": "b" * 64, "git_dirty": False,
    })
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    options = ["--tasks", str(bank), "--out", str(out), "--human", "files",
               "--quality-review", "deferred", "--search-requests", "0"]
    if transport == "stream":
        options += ["--llm-transport", "stream"]
    if reasoning_effort is not None:
        options += ["--llm-reasoning-effort", reasoning_effort]
    args = cli.parse_args(options)
    assert await asyncio.wait_for(cli.run_suite(args, [case]), 15) == 0
    manifest = read(out / "test.json")
    assert manifest["dataset_kind"] == case.source_kind == "supplementary"
    entry = manifest["scenarios"][0]
    task = read(out / case.id / "task_1-result.json")
    assert hashlib.sha256(role_path.read_bytes()).hexdigest() == role_sha
    assert [role["name"] for role in entry["roles"]] == [name for name, _title in MEMBERS]
    assert entry["roles"] == [role.model_dump(mode="json") for role in case.roles]
    assert task["root_status"] == "DONE" and task["verified_goal_success"] is None
    assert task["root_machine_accepted"] is None
    assert task["quality_status"] == "awaiting_human_or_independent_review"
    assert manifest["automatic_quality_pass"] is False
    assert entry["search"]["provider_requests"] == 0
    assert [name for name, _result in observed_tools] == [
        "source_access_audit", "verify_artifact_quote",
    ]
    assert all(result.ok for _name, result in observed_tools)
    assert json.loads(observed_tools[-1][1].stdout)["lexical_match"] is True
    reference = task["artifact_quote_evidence"]
    evidence_raw = (out / case.id / reference["path"]).read_bytes()
    assert hashlib.sha256(evidence_raw).hexdigest() == reference["sha256"]
    evidence = json.loads(evidence_raw)
    assert evidence["snapshot_status"] == "validated" and evidence["counts"]["matched"] == 1
    assert evidence["observed_record_count"] == 1
    assert evidence["quotation_coverage"] is evidence["factually_correct"] is None
    assert evidence["quality_pass"] is None
    return out, entry, constructors, emitted


async def test_default_buffered_constructor_and_real_registered_tools_capture_evidence(
    tmp_path, monkeypatch,
):
    out, entry, constructors, emitted = await exercise(tmp_path, monkeypatch, transport="buffered")
    assert len(constructors) == 1 and constructors[0] == {
        "api_key": "offline-controller-key", "base_url": "https://offline.invalid",
        "model": "offline-requested-model", "timeout_s": 1, "max_retries": 2,
    }
    assert emitted == [] and "stream_transport" not in entry
    assert not (out / entry["scenario_id"] / "stream-transport.json").exists()


async def test_explicit_stream_factory_persists_actual_stub_attempts_without_false_correlation(
    tmp_path, monkeypatch,
):
    out, entry, constructors, emitted = await exercise(tmp_path, monkeypatch, transport="stream")
    assert len(constructors) == 1
    kwargs = constructors[0]
    assert kwargs["max_attempts"] == 1 and kwargs["retry_transport"] is False
    assert "max_retries" not in kwargs and "completion_cap" not in kwargs
    assert callable(kwargs["on_attempt"])
    assert kwargs["reasoning_effort"] is None
    sidecar = read(out / entry["scenario_id"] / "stream-transport.json")
    assert Path(entry["stream_transport"]).resolve() == (
        out / entry["scenario_id"] / "stream-transport.json"
    ).resolve()
    assert sidecar["attempts"] == emitted and sidecar["call_count"] == len(emitted) > 0
    assert sidecar["attempt_count"] == len(emitted)
    assert emitted[0]["usage_present"] is False and emitted[0]["usage_raw"] == {}
    assert entry["llm"]["total"]["usage_missing_calls"] == 1
    assert entry["llm"]["total"]["tokens_in"] is None
    assert all("phase" not in row for row in sidecar["attempts"])
    assert "independent" in " ".join(sidecar["notes"])
    assert "offline-controller-key" not in json.dumps(sidecar)
    transcript = [json.loads(line) for line in (
        out / entry["scenario_id"] / "dialogue.jsonl"
    ).read_text(encoding="utf-8").splitlines()]
    dialogue_ids = {row["call_id"] for row in transcript if row["event"] == "llm_request"}
    assert dialogue_ids.isdisjoint({row["call_id"] for row in emitted})


async def test_explicit_reasoning_none_is_forwarded_and_recorded_without_quality_approval(
    tmp_path, monkeypatch,
):
    out, entry, constructors, _emitted = await exercise(
        tmp_path, monkeypatch, transport="stream", reasoning_effort="none",
    )
    assert constructors[0]["reasoning_effort"] == "none"
    assert read(out / "test.json")["model"]["reasoning_effort"] == "none"
    assert read(out / entry["scenario_id"] / "stream-transport.json")["reasoning_effort"] == "none"


def test_reasoning_choice_requires_stream_before_environment_or_output(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(LLMSettings, "from_env", classmethod(forbid))
    out = tmp_path / "must-not-exist"
    with pytest.raises(SystemExit) as error:
        cli.main(["--llm-reasoning-effort", "none", "--out", str(out)])
    assert error.value.code == 2
    assert not out.exists()


def test_both_plan_selectors_are_call_free_without_env_or_stream_import(
    tmp_path, monkeypatch, capsys,
):
    bank, case, _roles, _sha = single_case(tmp_path)
    original_import = builtins.__import__

    def checked_import(name, *args, **kwargs):
        if name == "moumou.eval.school_stream":
            raise AssertionError("--plan imported the optional streaming client")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(LLMSettings, "from_env", classmethod(forbid))
    monkeypatch.setattr("moumou.config.load_dotenv", forbid)
    monkeypatch.setattr("httpx.AsyncClient", forbid)
    monkeypatch.setattr("httpx.Client", forbid)
    monkeypatch.setattr(builtins, "__import__", checked_import)
    for selected in ("buffered", "stream"):
        out = tmp_path / (selected + "-plan-output")
        assert cli.main(["--tasks", str(bank), "--scenario", case.id, "--plan",
                         "--out", str(out), "--llm-transport", selected]) == 0
        planned = json.loads(capsys.readouterr().out)
        assert planned["llm_transport"] == selected
        assert planned["scenarios"][0]["roles"] == [name for name, _title in MEMBERS]
        assert not out.exists()
    out = tmp_path / "reasoning-plan-output"
    assert cli.main(["--tasks", str(bank), "--scenario", case.id, "--plan",
                     "--out", str(out), "--llm-transport", "stream",
                     "--llm-reasoning-effort", "none"]) == 0
    assert json.loads(capsys.readouterr().out)["llm_reasoning_effort"] == "none"
    assert not out.exists()
