"""Draft offline regressions: native stores/tools, no model or external HTTP."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from test_eval_team import scenario
from test_quality_tools_cli import exercise, forbid, read, single_case

from moumou.adapters import FakeLLM
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.config import LLMSettings
from moumou.context import AgentContext, Compactor, TokenBudget
from moumou.contracts import Budget, ContextPolicy, ToolContext, ToolResult
from moumou.eval import cli
from moumou.eval.research_progress import TeacherResearchProgress
from moumou.eval.source_evidence import SourceEvidenceStore
from moumou.eval.team import configure_team, submit_teacher_task
from moumou.orchestrator import Engine, Settings
from moumou.tools.source import ReadSourceTool, SourceHistoryTool

URL = "https://example.test/evidence"


class Trace:
    def __init__(self):
        self.rows = []

    def record(self, **row):
        self.rows.append(row)


def setup(tmp_path):
    paths = {role: (tmp_path / role).resolve() for role in ("alpha", "beta")}
    for path in paths.values():
        path.mkdir()
    nodes = {key: SimpleNamespace(id=key, project_id="p", conversation_id="c", role=role)
             for key, role in (("t1", "alpha"), ("t2", "alpha"), ("tb", "beta"))}
    trace = Trace()
    owner = TeacherResearchProgress("p", "c")
    owner.bind(SimpleNamespace(get=nodes.get), paths, trace)
    ctx = ToolContext(agent_id="alpha", task_id="t1", workspace=paths["alpha"])
    store = SourceEvidenceStore(tmp_path / "evidence")
    return owner, ctx, store, trace, nodes, paths


def seed(store, ctx, count=1, *, large=False):
    for index in range(count):
        sources = [{"url": URL if not large else f"{URL}/{index}/{part}",
                    "title": "Local source", "snippet": "s" * (1800 if large else 20)}
                   for part in range(5 if large else 1)]
        store.record_search("fixture", ToolResult(ok=True, stdout=json.dumps(sources)),
                            ctx, request_id=f"request-{index}")


def native_reader(store, text):
    native = ReadSourceTool(store)
    fetches = []

    async def fetched(url, provenance):
        fetches.append(url)
        metadata = store.record_document(url, url, text.encode(), text,
                                         "text/plain", "Local fixture")
        return metadata, text

    native._fetch = fetched
    return native, fetches


async def test_compact_history_uses_native_clipped_cursor_and_explicit_refresh(tmp_path):
    owner, ctx, store, trace, _nodes, _paths = setup(tmp_path)
    seed(store, ctx, 10, large=True)
    native = SourceHistoryTool(store)
    wrapped = owner.wrap(native)
    original_schema = json.dumps(native.params_schema, sort_keys=True)
    native_first = await native.run({"limit": 30}, ctx)
    original = json.loads(native_first.stdout)
    index = json.loads((await wrapped.run({"limit": 30}, ctx)).stdout)
    assert original["next_offset"] is not None  # Real native 64 KB clipping.
    assert index["next_offset"] == original["next_offset"]
    assert len(index["events"]) == len(original["events"])
    assert len(json.dumps(index).encode()) < len(native_first.stdout.encode())
    end = json.loads((await wrapped.run({"offset": index["next_offset"], "limit": 30}, ctx)).stdout)
    assert end["next_offset"] is None
    duplicate = json.loads((await wrapped.run({"limit": 30}, ctx)).stdout)
    assert duplicate["status"] == "no_new_evidence"
    restored = await wrapped.run({"limit": 30, "refresh": True}, ctx)
    assert restored.stdout == native_first.stdout
    assert json.dumps(native.params_schema, sort_keys=True) == original_schema
    assert "refresh" not in native.params_schema["properties"]
    assert all("model receipt" in row["interpretation"] for row in trace.rows)


async def test_history_end_change_and_distinct_query_are_not_a_tool_lockout(tmp_path):
    owner, ctx, store, _trace, _nodes, _paths = setup(tmp_path)
    seed(store, ctx)
    wrapped = owner.wrap(SourceHistoryTool(store))
    first = json.loads((await wrapped.run({"offset": 1}, ctx)).stdout)
    again = json.loads((await wrapped.run({"offset": 99}, ctx)).stdout)
    assert first["next_offset"] is None and again["next_offset"] is None
    assert again["offset"] == 99 and again["status"] == "no_new_evidence"
    different = json.loads((await wrapped.run({"query": "other", "offset": 0}, ctx)).stdout)
    assert "status" not in different
    store.record_search("new", ToolResult(ok=True, stdout=json.dumps([
        {"url": URL + "/new", "title": "new", "snippet": "distinct evidence"},
    ])), ctx, request_id="new-real-request")
    changed = json.loads((await wrapped.run({"offset": 1}, ctx)).stdout)
    assert changed["total_events"] == 2 and "status" not in changed


async def test_native_read_audit_runs_each_time_but_exact_text_is_not_reexpanded(tmp_path):
    owner, ctx, store, trace, _nodes, _paths = setup(tmp_path)
    seed(store, ctx)
    native, fetches = native_reader(store, "abcdefghijklmno")
    wrapped = owner.wrap(native)
    args = {"url": URL, "offset": 0, "limit": 8}
    full = json.loads((await wrapped.run(args, ctx)).stdout)
    repeat = json.loads((await wrapped.run({**args, "url": URL + "#alias"}, ctx)).stdout)
    restored = json.loads((await wrapped.run({**args, "refresh": True}, ctx)).stdout)
    assert full["text"] == restored["text"] == "abcdefgh"
    assert repeat["text"] == "" and repeat["status"] == "no_new_evidence"
    assert repeat["metadata_sha256"] == full["metadata_sha256"]
    assert repeat["next_offset"] == full["next_offset"] == 8
    assert repeat["cached"] is True and repeat["prepared_text_characters"] == 8
    assert repeat["emitted_text_characters"] == 0 and len(fetches) == 1
    reads = [event for event in store.history(limit=30)["events"] if event["kind"] == "read"]
    assert len(reads) == 3  # Projection never skips native prepared-read receipts.
    assert [row["projected_text_characters"] for row in trace.rows] == [8, 0, 8]
    assert trace.rows[-1]["explicit_refresh"] is True
    assert "abcdefgh" not in json.dumps(trace.rows)


async def test_distinct_window_version_actor_and_phase_keep_their_valid_text(tmp_path):
    owner, ctx, store, _trace, _nodes, paths = setup(tmp_path)
    seed(store, ctx)
    native, _fetches = native_reader(store, "abcdefghijklmnop")
    wrapped = owner.wrap(native)
    args = {"url": URL, "offset": 0, "limit": 8}
    assert json.loads((await wrapped.run(args, ctx)).stdout)["text"] == "abcdefgh"
    assert json.loads((await wrapped.run({**args, "offset": 4}, ctx)).stdout)["text"] == "efghijkl"
    for task, actor in (("t2", "alpha"), ("tb", "beta")):
        caller = ToolContext(agent_id=actor, task_id=task, workspace=paths[actor])
        assert json.loads((await wrapped.run(args, caller)).stdout)["text"] == "abcdefgh"
    store.record_document(URL, URL, b"new version", "UPDATED original", "text/plain", "New")
    changed = json.loads((await wrapped.run(args, ctx)).stdout)
    assert changed["text"] == "UPDATED " and "status" not in changed
    failed = await wrapped.run({**args, "offset": 999999}, ctx)
    assert failed.ok is False and "exceeds" in failed.error


async def test_refresh_recovers_native_window_after_real_compaction(tmp_path):
    owner, ctx, store, _trace, _nodes, _paths = setup(tmp_path)
    seed(store, ctx)
    native, _fetches = native_reader(store, "A" * 12000)
    wrapped = owner.wrap(native)
    args = {"url": URL, "limit": 8000}
    original = await wrapped.run(args, ctx)
    context = AgentContext([{"role": "system", "content": "Original role and human answer"},
                            {"role": "user", "content": "Original complete task"}])
    context.add_assistant("", [ToolCall(id="r", name="read_source", args=args)])
    context.add_tool_result("r", original)
    compactor = Compactor(TokenBudget(), ContextPolicy(
        context_window=10000, max_output_tokens=1000, compact_trigger_tokens=1500,
        compact_target_tokens=1000, opening_budget_tokens=500,
    ))
    assert context.maybe_compact(compactor).applied
    assert "A" * 8000 not in context.messages()[-1]["content"]
    recovered = json.loads((await wrapped.run({**args, "refresh": True}, ctx)).stdout)
    assert recovered["text"] == "A" * 8000
    assert context.messages()[0]["content"] == "Original role and human answer"
    assert context.messages()[1]["content"] == "Original complete task"


async def test_cancel_after_native_preparation_does_not_claim_wrapper_projection(tmp_path):
    owner, ctx, store, trace, _nodes, _paths = setup(tmp_path)
    seed(store, ctx)
    native, _fetches = native_reader(store, "abcdefghijklmno")
    original_run = native.run
    prepared, blocked = asyncio.Event(), asyncio.Event()

    async def prepare_then_block(args, caller):
        result = await original_run(args, caller)
        prepared.set()
        await blocked.wait()
        return result

    native.run = prepare_then_block
    wrapped = owner.wrap(native)
    args = {"url": URL, "limit": 8}
    job = asyncio.create_task(wrapped.run(args, ctx))
    await asyncio.wait_for(prepared.wait(), 1)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert trace.rows == [] and not owner._states
    native.run = original_run
    later = json.loads((await wrapped.run(args, ctx)).stdout)
    assert later["text"] == "abcdefgh" and "status" not in later
    assert trace.rows[-1]["repeated_evidence"] is False
    assert len([event for event in store.history(limit=30)["events"]
                if event["kind"] == "read"]) == 2


async def test_scope_and_refresh_attack_never_invoke_native_and_ledger_is_bounded(tmp_path):
    owner, ctx, store, trace, nodes, paths = setup(tmp_path)
    seed(store, ctx)

    class Counted(SourceHistoryTool):
        calls = 0

        async def run(self, args, ctx):
            self.calls += 1
            return await super().run(args, ctx)

    native = Counted(store)
    wrapped = owner.wrap(native)
    for refresh in (None, 1, "true", []):
        assert not (await wrapped.run({"refresh": refresh}, ctx)).ok
    wrong = ctx.model_copy(update={"agent_id": "beta"})
    assert not (await wrapped.run({}, wrong)).ok
    assert native.calls == 0
    for index in range(160):
        task = f"extra-{index}"
        nodes[task] = SimpleNamespace(id=task, project_id="p", conversation_id="c", role="alpha")
        caller = ToolContext(agent_id="alpha", task_id=task, workspace=paths["alpha"])
        await wrapped.run({}, caller)
    for index in range(160):
        await wrapped.run({"query": f"different-{index}"}, ctx)
    assert native.calls == 320  # Exactly one native invocation per valid request.
    assert len(owner._states) <= 128
    assert all(len(state["identities"]) <= 128 for state in owner._states.values())
    assert len(trace.rows) == 320


async def test_real_native_retry_receives_checkpoint_without_disabling_memory(tmp_path):
    case = scenario(tmp_path)
    owner = TeacherResearchProgress(case.id, "instructor-group")
    store = SourceEvidenceStore(tmp_path / "source-evidence")
    checkpoint_requests = []

    def script(messages, tools):
        names = {spec["function"]["name"] for spec in tools or []}
        if names == {"submit_lesson"}:
            return LLMResponse(tool_calls=[
                ToolCall(id="l", name="submit_lesson", args={"lessons": []}),
            ])
        has_checkpoint = "## This task's prepared evidence checkpoint" in messages[0]["content"]
        checkpoint_requests.append(has_checkpoint)
        if has_checkpoint:
            return LLMResponse(tool_calls=[ToolCall(id="done", name="submit_result", args={
                "summary": "Synthetic source inspection complete", "files": [],
            })], tokens_in=10, usage_present=True)
        return LLMResponse(tool_calls=[ToolCall(id="first", name="source_history", args={})],
                           tokens_in=1100, usage_present=True)

    with Engine(FakeLLM(script), settings=Settings(
            root=tmp_path / "run", project_id=case.id, conversation_id="instructor-group",
            memory_enabled=True, role_review_enabled=False)) as run:
        run.tools.register_all(owner.wrap(SourceHistoryTool(store)))
        team = configure_team(run, case)
        owner.bind(run.repo, team.workspaces, run.trace)
        team.context.research_progress = owner
        compactor = run.runner.compactor
        root = submit_teacher_task(run, case, "Inspect the local synthetic source index", Budget(
            max_tokens=1000, max_steps=3, max_retries=1,
        ))
        await run.run()
        assert run.repo.get(root.id).status == "DONE"
        assert run.repo.get(root.id).retries == 1
        assert checkpoint_requests == [False, True]
        assert run.runner.compactor is compactor
        assert run.settings.memory_enabled is True


@pytest.mark.parametrize("enabled", [False, True])
async def test_actual_cli_wiring_records_opt_in_and_default_stays_unwrapped(
    tmp_path, monkeypatch, enabled,
):
    original_parse, original_wrap = cli.parse_args, TeacherResearchProgress.wrap
    wrapped = []

    def configured(options):
        return original_parse([*options, *(["--research-progress"] if enabled else [])])

    def observed(self, native):
        wrapped.append(native.name)
        return original_wrap(self, native)

    monkeypatch.setattr(cli, "parse_args", configured)
    monkeypatch.setattr(TeacherResearchProgress, "wrap", observed)
    output, _entry, constructors, _attempts = await exercise(
        tmp_path, monkeypatch, transport="buffered",
    )
    assert wrapped == (["source_history", "read_source"] if enabled else [])
    assert read(output / "test.json")["research_progress_policy"]["enabled"] is enabled
    assert "research_progress" not in constructors[0]


async def test_invalid_programmatic_mode_precedes_environment_and_output(tmp_path, monkeypatch):
    bank, case, _roles, _sha = single_case(tmp_path)
    output = tmp_path / "no-output"
    args = cli.parse_args(["--tasks", str(bank), "--out", str(output)])
    args.research_progress = "yes"
    monkeypatch.setattr(LLMSettings, "from_env", classmethod(forbid))
    with pytest.raises(ValueError, match="research_progress"):
        await cli.run_suite(args, [case])
    assert not output.exists()


def test_progress_plan_needs_no_environment_or_output(tmp_path, monkeypatch, capsys):
    bank, _case, _roles, _sha = single_case(tmp_path)
    output = tmp_path / "no-plan-output"
    monkeypatch.setattr(LLMSettings, "from_env", classmethod(forbid))
    assert cli.main(["--tasks", str(bank), "--out", str(output), "--plan",
                     "--research-progress"]) == 0
    assert json.loads(capsys.readouterr().out)["research_progress"] is True
    assert not output.exists()
