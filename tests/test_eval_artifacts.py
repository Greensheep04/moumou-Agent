"""Local D04 regressions for independent batches and recoverable run evidence."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from moumou.adapters import FakeLLM
from moumou.adapters.llm import LLMResponse
from moumou.config import LLMSettings
from moumou.contracts import Acceptance, Budget, MemoryRecord, TraceEvent
from moumou.eval import artifacts, cli, runner
from moumou.eval.artifacts import atomic_json, create_manifest, load_results
from moumou.eval.loader import TaskFixture
from moumou.eval.runner import render_markdown, run_all
from moumou.eval.workspace import reserve_batch_dir
from moumou.memory import SqliteMemoryStore
from moumou.store import SqliteTraceStore


def fixture(fixture_id="sample", *, machine=True, content="correct", **changes):
    values = {
        "id": fixture_id,
        "title": "Offline artifact evidence",
        "scenario": "from_scratch",
        "goal": "Verify the seeded answer file",
        "setup": {"answer.txt": content},
        "acceptance": [
            Acceptance(kind="file_contains", target="answer.txt", expect=r"^correct$")
        ] if machine else [],
        "budget": Budget(max_steps=1, max_retries=0, max_tokens=1000),
    }
    values.update(changes)
    return TaskFixture(**values)


def model():
    return FakeLLM(lambda messages, tools: LLMResponse(
        content="The answer file is ready", tokens_in=7, tokens_out=3
    ))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def journal(root):
    return [json.loads(line) for line in (root / "results.jsonl").read_text(
        encoding="utf-8"
    ).splitlines() if line.strip()]


def workspace(result):
    return Path(result.run_dir) / "workspaces" / result.fixture_id / "c1" / "planner@t1"


async def test_two_batches_and_repeats_keep_all_state_independent(tmp_path):
    roots = [tmp_path / "first", tmp_path / "second"]
    groups = [await run_all([fixture(repeats=2)], model(), root) for root in roots]
    results = [item for group in groups for item in group]
    assert len(results) == 4
    assert len({item.run_dir for item in results}) == 4
    manifests = [read_json(root / "manifest.json") for root in roots]
    assert manifests[0]["eval_id"] != manifests[1]["eval_id"]
    for root, group, manifest in zip(roots, groups, manifests, strict=True):
        assert manifest["planned_runs"] == manifest["completed_runs"] == 2
        assert manifest["status"] == "completed"
        assert len(journal(root)) == len(load_results(root)) == 2
        assert (root / "report.md").is_file()
        for result in group:
            assert result.eval_id == manifest["eval_id"]
            assert result.root_success is True
            assert result.done == result.total == 1
            run = Path(result.run_dir)
            for name in ("moumou.db", "memory.db", "trace.db", "trace.jsonl", "result.json"):
                assert (run / name).is_file(), name
            assert read_json(run / "result.json") == asdict(result)
            assert (workspace(result) / "answer.txt").read_text(encoding="utf-8") == "correct"

    first, *others = results
    (workspace(first) / "answer.txt").write_text("mutated", encoding="utf-8")
    with closing(sqlite3.connect(Path(first.run_dir) / "moumou.db")) as db:
        db.execute("UPDATE tasks SET goal = 'mutated only here' WHERE id = 't1'")
        db.commit()
    memory = SqliteMemoryStore(Path(first.run_dir) / "memory.db")
    try:
        memory_id = memory.write(MemoryRecord(
            project_id=first.fixture_id, role="planner", task_id="t1",
            situation="isolated marker", action="write marker", outcome="success",
            lesson="Only the first run may contain this record",
        ))
    finally:
        memory.close()
    marker = TraceEvent(task_id="t1", kind="tool", name="first-run-only")
    with SqliteTraceStore(Path(first.run_dir) / "trace.db") as store:
        store.append(marker)
    for result in others:
        assert (workspace(result) / "answer.txt").read_text(encoding="utf-8") == "correct"
        with closing(sqlite3.connect(Path(result.run_dir) / "moumou.db")) as db:
            assert db.execute("SELECT goal FROM tasks WHERE id='t1'").fetchone()[0] != (
                "mutated only here"
            )
        with closing(sqlite3.connect(Path(result.run_dir) / "memory.db")) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM memories WHERE id=?", (memory_id,)
            ).fetchone()[0]
            assert count == 0
        with SqliteTraceStore(Path(result.run_dir) / "trace.db") as store:
            assert all(event.span_id != marker.span_id for event in store.query("t1"))


@pytest.mark.parametrize(
    "machine,content,status,machine_accepted,root_success",
    [
        (True, "correct", "succeeded", True, True),
        (False, "correct", "unverified", None, None),
        (True, "wrong", "failed", False, False),
    ],
)
async def test_result_distinguishes_machine_verdicts_from_self_report(
    tmp_path, machine, content, status, machine_accepted, root_success
):
    result, = await run_all([fixture(machine=machine, content=content)], model(), tmp_path)
    saved = read_json(Path(result.run_dir) / "result.json")
    assert result.status == saved["status"] == status
    assert result.root_task_id == "t1"
    assert result.root_machine_accepted is machine_accepted
    assert result.root_success is root_success
    assert result.root_status == ("DEAD" if status == "failed" else "DONE")
    assert saved["nodes"] and saved["nodes"][0]["id"] == "t1"
    assert result.elapsed_s >= result.wall_s >= 0
    assert result.started_at and result.finished_at
    assert saved["usage_missing_calls"] is None
    assert saved["total_tokens_in"] is None and saved["total_tokens_out"] is None
    if status == "failed":
        assert result.error_type == "acceptance_failed"
        assert result.dead == 1 and result.done == 0
    assert journal(tmp_path) == [saved]


async def test_bad_setup_is_a_saved_failure_and_the_next_fixture_runs(tmp_path):
    bad = fixture("bad", setup={"../../escape.txt": "must not be written"})
    results = await run_all([bad, fixture("next")], model(), tmp_path)
    assert [result.status for result in results] == ["error", "succeeded"]
    assert results[0].error_type == "setup_error"
    assert results[0].wall_s is None
    assert results[0].tokens_in is None and results[0].tokens_out is None
    assert not list(tmp_path.rglob("escape.txt"))
    assert not (tmp_path.parent / "escape.txt").exists()
    assert len(journal(tmp_path)) == 2
    assert read_json(Path(results[0].run_dir) / "result.json")["error_type"] == "setup_error"
    assert read_json(tmp_path / "manifest.json")["completed_runs"] == 2


async def test_engine_exception_is_recorded_and_later_runs_continue(tmp_path, monkeypatch):
    original = runner.Engine.run
    fake = model()
    fake.api_key = "sk-local-only-placeholder-secret"

    async def fail_one(engine):
        if engine.settings.project_id == "crash":
            raise RuntimeError(f"injected engine failure with {fake.api_key}")
        return await original(engine)

    monkeypatch.setattr(runner.Engine, "run", fail_one)
    results = await run_all([fixture("crash"), fixture("next")], fake, tmp_path)
    failed, succeeded = results
    assert failed.status == "error" and failed.error_type == "worker_crash"
    assert "injected engine failure" in failed.error_message
    assert fake.api_key not in (tmp_path / "results.jsonl").read_text(encoding="utf-8")
    assert failed.wall_s is None and failed.tokens_in is None and failed.tokens_out is None
    assert succeeded.status == "succeeded" and succeeded.root_success is True
    assert len(load_results(tmp_path)) == 2


async def test_batch_cancel_preserves_finished_run_and_current_cancel_record(tmp_path, monkeypatch):
    original = runner.Engine.run
    entered = asyncio.Event()

    async def pause_second(engine):
        if engine.settings.project_id == "pause":
            entered.set()
            await asyncio.Event().wait()  # No scheduler/worker was started at this seam.
        return await original(engine)

    monkeypatch.setattr(runner.Engine, "run", pause_second)
    task = asyncio.create_task(run_all(
        [fixture("ready"), fixture("pause"), fixture("never")], model(), tmp_path
    ))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        first_path = tmp_path / "ready/r0/result.json"
        original_first = first_path.read_bytes()
        assert journal(tmp_path)[0]["fixture_id"] == "ready"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert first_path.read_bytes() == original_first
        results = load_results(tmp_path)
        assert [item["fixture_id"] for item in results] == ["ready", "pause"]
        assert results[0]["status"] == "succeeded"
        assert results[1]["status"] == "cancelled"
        assert results[1]["error_type"] == "cancelled"
        assert len(journal(tmp_path)) == 2
        manifest = read_json(tmp_path / "manifest.json")
        assert manifest["status"] == "interrupted" and manifest["finished_at"]
        assert manifest["planned_runs"] == 3 and manifest["completed_runs"] == 2
        assert not (tmp_path / "never/r0").exists()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("damage", ["missing", "partial_tail"])
async def test_per_run_results_survive_a_missing_or_torn_jsonl_index(tmp_path, damage):
    results = await run_all([fixture(repeats=2)], model(), tmp_path)
    expected = [asdict(result) for result in results]
    index = tmp_path / "results.jsonl"
    if damage == "missing":
        index.unlink()
    else:
        with index.open("a", encoding="utf-8") as stream:
            stream.write('{"fixture_id": "unfinished')
    assert load_results(tmp_path) == expected


async def test_index_write_failure_keeps_the_completed_authoritative_result(tmp_path, monkeypatch):
    def fail_append(root, data):
        raise OSError("injected index write failure")

    monkeypatch.setattr(runner, "append_result", fail_append)
    with pytest.raises(OSError, match="injected index write failure"):
        await run_all([fixture("first"), fixture("later")], model(), tmp_path)
    recovered = load_results(tmp_path)
    assert len(recovered) == 1 and recovered[0]["fixture_id"] == "first"
    assert recovered[0]["status"] == "succeeded"
    assert not (tmp_path / "later/r0").exists()
    assert read_json(tmp_path / "manifest.json")["status"] != "completed"


async def test_reusing_a_batch_does_not_overwrite_previous_evidence(tmp_path):
    await run_all([fixture()], model(), tmp_path)
    before = {path.relative_to(tmp_path): path.read_bytes()
              for path in tmp_path.rglob("*") if path.is_file()}
    fake = model()
    with pytest.raises(FileExistsError):
        await run_all([fixture("different")], fake, tmp_path)
    assert fake.calls == []
    after = {path.relative_to(tmp_path): path.read_bytes()
             for path in tmp_path.rglob("*") if path.is_file()}
    assert before == after


async def test_colliding_attempt_is_not_silently_loaded_as_a_completed_run(tmp_path, monkeypatch):
    saved = {}

    async def collide(task, llm, root, repeat, **kwargs):
        directory = root / task.id / f"r{repeat}"
        directory.mkdir(parents=True)
        stale = runner.FixtureResult(
            fixture_id=task.id, scenario=task.scenario, repeat=repeat,
            done=1, dead=0, total=1, completion_rate=1.0, wall_s=1.0,
            tokens_in=7, tokens_out=3, run_dir=str(directory),
            eval_id=kwargs["eval_id"], status="succeeded",
        )
        path = directory / "result.json"
        atomic_json(path, asdict(stale))
        saved["path"], saved["bytes"] = path, path.read_bytes()
        raise FileExistsError("injected existing attempt")

    monkeypatch.setattr(runner, "run_once", collide)
    with pytest.raises(FileExistsError, match="injected existing attempt"):
        await run_all([fixture()], model(), tmp_path)
    assert saved["path"].read_bytes() == saved["bytes"]
    assert journal(tmp_path) == []
    manifest = read_json(tmp_path / "manifest.json")
    assert manifest["status"] == "error" and manifest["completed_runs"] == 0


async def test_exception_cannot_import_another_batch_result(tmp_path, monkeypatch):
    saved = {}

    async def fail_with_foreign_result(task, llm, root, repeat, **kwargs):
        directory = root / task.id / f"r{repeat}"
        directory.mkdir(parents=True)
        foreign = runner.FixtureResult(
            fixture_id=task.id, scenario=task.scenario, repeat=repeat,
            done=1, dead=0, total=1, completion_rate=1.0, wall_s=1.0,
            tokens_in=7, tokens_out=3, run_dir=str(directory),
            eval_id="another-batch", status="succeeded",
        )
        path = directory / "result.json"
        atomic_json(path, asdict(foreign))
        saved["path"], saved["bytes"] = path, path.read_bytes()
        raise RuntimeError("injected failure beside foreign evidence")

    monkeypatch.setattr(runner, "run_once", fail_with_foreign_result)
    with pytest.raises(ValueError, match="does not belong"):
        await run_all([fixture()], model(), tmp_path)
    assert saved["path"].read_bytes() == saved["bytes"]
    assert journal(tmp_path) == []
    manifest = read_json(tmp_path / "manifest.json")
    assert manifest["status"] == "error" and manifest["completed_runs"] == 0


async def test_cli_default_creates_fresh_batches_and_closes_each_client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    (task_dir / "cli.yaml").write_text(
        json.dumps(fixture("cli").model_dump(mode="json")), encoding="utf-8"
    )
    clients = []

    def make_client(**kwargs):
        fake = model()
        fake.aclose = AsyncMock()
        clients.append(fake)
        return fake

    monkeypatch.setattr(cli, "DeepSeekClient", make_client)
    monkeypatch.setattr(cli.LLMSettings, "from_env", classmethod(lambda cls: LLMSettings(
        api_key="offline-test-placeholder", model="offline-model",
        base_url="https://api.invalid", timeout_s=1,
    )))
    args = cli.parse_args(["--tasks", str(task_dir)])
    assert args.out is None
    assert await cli.main_async(args) == 0
    assert await cli.main_async(args) == 0
    paths = sorted((tmp_path / "runs/eval").glob("*/manifest.json"))
    assert len(paths) == len(clients) == 2
    manifests = [read_json(path) for path in paths]
    assert manifests[0]["eval_id"] != manifests[1]["eval_id"]
    assert paths[0].parent != paths[1].parent
    for path, manifest in zip(paths, manifests, strict=True):
        assert manifest["status"] == "completed"
        records = load_results(path.parent)
        assert len(records) == 1 and records[0]["root_success"] is True
    for fake in clients:
        assert fake.calls
        fake.aclose.assert_awaited_once()


@pytest.mark.parametrize(
    "fixtures,repeats",
    [
        ([], None),
        ([fixture("duplicate"), fixture("duplicate")], None),
        ([fixture("Case"), fixture("case")], None),
        ([fixture()], 0),
        ([fixture()], -1),
        ([fixture()], True),
        ([fixture("../outside")], None),
    ],
)
async def test_invalid_batch_plan_fails_before_model_calls(tmp_path, fixtures, repeats):
    fake = model()
    with pytest.raises(ValueError):
        await run_all(fixtures, fake, tmp_path / "batch", repeats=repeats)
    assert fake.calls == []
    assert not (tmp_path / "outside").exists()


def test_manifest_identifies_source_and_normalized_fixture_without_recording_keys(tmp_path):
    fake = model()
    fake.api_key = "sk-local-only-placeholder-secret"
    fake.base_url = "https://user:password@api.invalid/v1?api_key=another-secret"
    task = fixture()

    def manifest(name, item):
        root = reserve_batch_dir(tmp_path / name)
        return create_manifest(root, [item], fake, 1, "local")

    first = manifest("first", task)
    same = manifest("same", task)
    changed = manifest("changed", task.model_copy(update={"goal": "A different goal"}))
    assert first["schema_version"] == 1
    assert first["purpose"] == "offline_validation"
    assert first["code"]["git_commit"]
    assert len(first["code"]["source_sha256"]) == 64
    assert isinstance(first["code"]["git_dirty"], bool)
    assert first["code"]["source_sha256"] == same["code"]["source_sha256"]
    assert first["dataset"]["sha256"] == same["dataset"]["sha256"]
    assert first["dataset"]["sha256"] != changed["dataset"]["sha256"]
    assert first["dataset"]["fixtures"][0]["sha256"] != (
        changed["dataset"]["fixtures"][0]["sha256"]
    )
    assert len({item["eval_id"] for item in (first, same, changed)}) == 3
    serialized = json.dumps(first)
    for secret in (fake.api_key, "password", "another-secret", "api_key"):
        assert secret not in serialized
    assert first["model"]["provider"] == "fake"
    assert first["settings"]["harness_concurrency"] == 1
    assert first["settings"]["workspace_backend"] == "local"


def test_model_manifest_keeps_public_provider_settings_without_url_credentials(tmp_path):
    secret = "sk-local-only-placeholder-secret"
    client = SimpleNamespace(
        api_key=secret,
        base_url="https://user:password@api1.hkgai.net/v1?api_key=query-secret",
        model="course-model", timeout_s=180, max_retries=3,
    )
    root = reserve_batch_dir(tmp_path / "batch")
    manifest = create_manifest(root, [fixture()], client, 1, "local")
    assert manifest["purpose"] == "model_evaluation"
    assert manifest["model"]["provider"] == "hkgai"
    assert manifest["model"]["endpoint_host"] == "api1.hkgai.net"
    assert manifest["model"]["model"] == "course-model"
    assert manifest["model"]["timeout_s"] == 180
    serialized = json.dumps(manifest)
    assert all(value not in serialized for value in (secret, "password", "query-secret"))


async def test_explicit_eval_id_is_preserved_in_every_record(tmp_path):
    result, = await run_all([fixture()], model(), tmp_path, eval_id="review-batch-01")
    assert result.eval_id == "review-batch-01"
    assert read_json(tmp_path / "manifest.json")["eval_id"] == result.eval_id
    assert journal(tmp_path)[0]["eval_id"] == result.eval_id
    assert load_results(tmp_path)[0]["eval_id"] == result.eval_id


def test_atomic_json_failure_keeps_previous_evidence_and_removes_temporary(tmp_path, monkeypatch):
    path = tmp_path / "result.json"
    atomic_json(path, {"state": "old"})
    before = path.read_bytes()

    def fail_replace(src, dst):
        raise OSError("injected replace failure")

    monkeypatch.setattr(artifacts.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        atomic_json(path, {"state": "new"})
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("damage", ["batch_identity", "path_escape"])
async def test_loading_results_refuses_cross_batch_identity_or_escaping_paths(tmp_path, damage):
    result, = await run_all([fixture()], model(), tmp_path)
    if damage == "batch_identity":
        path = Path(result.run_dir) / "result.json"
        data = read_json(path)
        data["eval_id"] = "some-other-batch"
        atomic_json(path, data)
        error = ValueError
    else:
        path = tmp_path / "manifest.json"
        data = read_json(path)
        data["runs"][0]["run_dir"] = "../outside"
        atomic_json(path, data)
        error = PermissionError
    with pytest.raises(error):
        load_results(tmp_path)


def test_empty_report_is_readable_without_dividing_by_zero():
    report = render_markdown([])
    assert isinstance(report, str) and "0" in report
