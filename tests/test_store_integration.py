"""Persistent traces through Recorder and actual deterministic Engine runs."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from moumou.adapters import FakeLLM
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.contracts import Acceptance, TraceEvent
from moumou.orchestrator import Engine, Settings
from moumou.store import SqliteTraceStore, TraceImportError
from moumou.trace import Recorder

BASE = datetime(2026, 11, 8, tzinfo=UTC)


def event(span_id: str, *, seconds: int = 0, task_id: str = "task-1") -> TraceEvent:
    return TraceEvent(
        span_id=span_id,
        task_id=task_id,
        parent_span="parent",
        agent_id="writer@task-1",
        kind="llm",
        name="中文事件",
        t0=BASE + timedelta(seconds=seconds),
        t1=BASE + timedelta(seconds=seconds, microseconds=17),
        status="ok",
        tokens_in=13,
        tokens_out=7,
        payload={"result": {"文字": "完整保存", "items": [1, None, True]}},
    )


def read_jsonl(path: Path) -> list[TraceEvent]:
    return [
        TraceEvent.model_validate_json(line)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]


def by_span(events: list[TraceEvent]) -> dict[str, dict]:
    return {item.span_id: item.model_dump() for item in events}


def writer_model(path: str) -> FakeLLM:
    def respond(messages, tools):
        if not any(message["role"] == "assistant" for message in messages):
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="write",
                        name="write_file",
                        args={"path": path, "content": "持久化任务产物"},
                    )
                ],
                tokens_in=11,
                tokens_out=3,
            )
        return LLMResponse(content=f"wrote {path}", tokens_in=7, tokens_out=5)

    return FakeLLM(respond)


def engine_settings(root: Path) -> Settings:
    return Settings(
        project_id="p",
        conversation_id="c",
        root=root,
        memory_enabled=False,
        role_review_enabled=False,
    )


async def test_engine_task_writes_matching_jsonl_and_sqlite_traces(tmp_path):
    root = tmp_path / "run"
    with Engine(writer_model("out.txt"), settings=engine_settings(root)) as engine:
        task = engine.submit(
            "write out.txt", acceptance=[Acceptance(kind="file_exists", target="out.txt")]
        )
        report = await engine.run()
        workspace = engine.workspace.allocate("p", "c", f"{task.role}@{task.id}")
        assert (workspace / "out.txt").read_text(encoding="utf-8") == "持久化任务产物"
        assert report.done == 1 and report.dead == 0
        assert report.tokens_in == 18 and report.tokens_out == 8
        assert engine.trace.projection_error is None
        queried = engine.trace.query(task.id)
        assert {item.kind for item in queried} >= {"llm", "tool", "acceptance"}

    replay = [item for item in read_jsonl(root / "trace.jsonl") if item.task_id == task.id]
    with SqliteTraceStore(root / "trace.db") as store:
        persisted = store.query(task.id)
    assert by_span(persisted) == by_span(replay) == by_span(queried)
    assert len(persisted) == len(replay) == len(queried)


async def test_reopened_engine_keeps_old_events_and_persists_the_next_task(tmp_path):
    root = tmp_path / "run"
    settings = engine_settings(root)
    with Engine(writer_model("first.txt"), settings=settings) as engine:
        first = engine.submit(
            "first file", acceptance=[Acceptance(kind="file_exists", target="first.txt")]
        )
        assert (await engine.run()).done == 1
        previous = engine.trace.query(first.id)

    with Engine(writer_model("second.txt"), settings=settings) as engine:
        assert engine.trace.query(first.id) == previous
        assert engine.repo.get(first.id).status == "DONE"
        second = engine.submit(
            "second file", acceptance=[Acceptance(kind="file_exists", target="second.txt")]
        )
        assert second.id != first.id
        report = await engine.run()
        assert report.total == report.done == 2
        assert engine.trace.query(first.id) == previous
        following = engine.trace.query(second.id)
        assert following

    with SqliteTraceStore(root / "trace.db") as store:
        assert store.query(first.id) == previous
        assert store.query(second.id) == following


async def test_concurrent_tasks_keep_their_traces_isolated_and_complete(tmp_path):
    first_turns: set[str] = set()
    both_started = asyncio.Event()

    def respond(messages, tools):
        filename = "first.txt" if "first.txt" in messages[1]["content"] else "second.txt"
        if not any(message["role"] == "assistant" for message in messages):
            return LLMResponse(tool_calls=[ToolCall(
                id="write", name="write_file", args={"path": filename, "content": filename}
            )])
        return LLMResponse(content=f"wrote {filename}")

    class ConcurrentWriter(FakeLLM):
        async def chat(self, messages, tools=None):
            if not any(message["role"] == "assistant" for message in messages):
                filename = "first.txt" if "first.txt" in messages[1]["content"] else "second.txt"
                first_turns.add(filename)
                if len(first_turns) == 2:
                    both_started.set()
                await asyncio.wait_for(both_started.wait(), timeout=5)
            return await super().chat(messages, tools)

    root = tmp_path / "concurrent"
    settings = engine_settings(root).model_copy(update={"max_slots": 2})
    with Engine(ConcurrentWriter(respond), settings=settings) as engine:
        tasks = [
            engine.submit(
                f"write {filename}",
                acceptance=[Acceptance(kind="file_contains", target=filename, expect=filename)],
            )
            for filename in ("first.txt", "second.txt")
        ]
        report = await engine.run()
        assert first_turns == {"first.txt", "second.txt"}
        assert report.total == report.done == 2 and report.dead == 0
        queried = {task.id: engine.trace.query(task.id) for task in tasks}
        for task in tasks:
            assert queried[task.id]
            assert {item.task_id for item in queried[task.id]} == {task.id}

    replay = read_jsonl(root / "trace.jsonl")
    with SqliteTraceStore(root / "trace.db") as store:
        persisted = [item for task in tasks for item in store.query(task.id)]
    assert len({item.span_id for item in replay}) == len(replay)
    assert len(replay) == len(persisted)
    assert by_span(replay) == by_span(persisted)
    assert by_span(persisted) == by_span([item for rows in queried.values() for item in rows])


@pytest.mark.parametrize("with_bom", [False, True])
def test_existing_jsonl_is_recovered_without_rewriting_the_source(tmp_path, with_bom):
    source, database = tmp_path / "trace.jsonl", tmp_path / "trace.db"
    later, earlier, latest = event("later", seconds=2), event("earlier"), event("latest", seconds=3)
    with Recorder(source) as recorder:
        recorder.append(later)
        recorder.append(earlier)
        assert recorder.projection_error is None
        assert recorder.all_events() == [later, earlier]
    assert not database.exists(), "the one-argument Recorder remains JSONL-only"
    if with_bom:
        source.write_bytes(b"\xef\xbb\xbf" + source.read_bytes())
    original = source.read_bytes()

    with Recorder(source, sqlite_path=database) as recorder:
        assert source.read_bytes() == original
        assert recorder.query("task-1") == [earlier, later]
        assert recorder.all_events() == [later, earlier]
        recorder.append(latest)
        assert recorder.query("task-1") == [earlier, later, latest]
        assert recorder.query("another-task") == []

    assert source.read_bytes().startswith(original)
    with SqliteTraceStore(database) as store:
        assert store.query("task-1") == [earlier, later, latest]


def test_partial_projection_is_repaired_and_reopening_is_idempotent(tmp_path):
    source, database = tmp_path / "trace.jsonl", tmp_path / "trace.db"
    first, second = event("first"), event("second", seconds=1)
    with Recorder(source) as recorder:
        recorder.append(first)
        recorder.append(second)
    with SqliteTraceStore(database) as store:
        store.append(first)
    original = source.read_bytes()

    for _ in range(2):
        with Recorder(source, sqlite_path=database) as recorder:
            assert recorder.query("task-1") == [first, second]
            assert recorder.all_events() == [first, second]
    assert source.read_bytes() == original
    with SqliteTraceStore(database) as store:
        assert store.query("task-1") == [first, second]


def test_nested_spans_keep_replay_order_and_query_in_start_order(tmp_path):
    source, database = tmp_path / "trace.jsonl", tmp_path / "trace.db"
    with Recorder(source, sqlite_path=database) as recorder:
        with recorder.span(task_id="task-1", kind="task", name="outer") as outer:
            outer.t0 = BASE
            with recorder.span(task_id="task-1", kind="tool", name="inner") as inner:
                inner.t0 = BASE + timedelta(seconds=1)
                inner.parent_span = outer.span_id
                inner.payload["output"] = "saved"
        assert recorder.all_events() == [inner, outer]
        assert recorder.query("task-1") == [outer, inner]

    with SqliteTraceStore(database) as store:
        persisted = store.query("task-1")
        assert persisted == [outer, inner]
        assert persisted[1].parent_span == persisted[0].span_id


def test_projection_write_failure_keeps_complete_sorted_reads_and_recovers(
    tmp_path, monkeypatch, caplog
):
    source, database = tmp_path / "trace.jsonl", tmp_path / "trace.db"
    first, later, earlier = event("first", seconds=1), event("later", seconds=2), event("earlier")
    failure = sqlite3.OperationalError("projection unavailable")

    def unavailable(self, item):
        raise failure

    caplog.set_level(logging.WARNING, logger="moumou.trace.recorder")
    with Recorder(source, sqlite_path=database) as recorder:
        recorder.append(first)
        with monkeypatch.context() as patch:
            patch.setattr(SqliteTraceStore, "append", unavailable)
            recorder.append(later)
            recorder.append(earlier)
        assert recorder.projection_error is failure
        assert recorder.query("task-1") == [earlier, first, later]
        assert recorder.all_events() == [first, later, earlier]
        with SqliteTraceStore(database) as store:
            assert store.query("task-1") == [first]

    warnings = [
        item for item in caplog.records
        if item.name == "moumou.trace.recorder" and item.levelno >= logging.WARNING
    ]
    assert len(warnings) == 1
    assert read_jsonl(source) == [first, later, earlier]
    with Recorder(source, sqlite_path=database) as recorder:
        assert recorder.projection_error is None
        assert recorder.query("task-1") == [earlier, first, later]


@pytest.mark.parametrize(
    ("original", "status"),
    [(RuntimeError("business failure"), "error"), (asyncio.CancelledError("stop"), "cancelled")],
)
def test_projection_failure_does_not_replace_business_error_or_cancellation(
    tmp_path, monkeypatch, original, status
):
    source, database = tmp_path / "trace.jsonl", tmp_path / "trace.db"

    def unavailable(self, item):
        raise sqlite3.OperationalError("projection unavailable")

    with Recorder(source, sqlite_path=database) as recorder:
        with monkeypatch.context() as patch:
            patch.setattr(SqliteTraceStore, "append", unavailable)
            with pytest.raises(type(original)) as caught:
                with recorder.span(task_id="task-1", kind="tool", name="failed"):
                    raise original
        assert caught.value is original
        assert isinstance(recorder.projection_error, sqlite3.OperationalError)
        events = recorder.query("task-1")
        assert len(events) == 1 and events[0].status == status
        assert events[0].t1 is not None

    with Recorder(source, sqlite_path=database) as recorder:
        assert recorder.query("task-1") == events


def test_projection_read_failure_falls_back_without_losing_later_events(tmp_path, monkeypatch):
    source, database = tmp_path / "trace.jsonl", tmp_path / "trace.db"
    later, earlier = event("later", seconds=2), event("earlier")
    failure = sqlite3.OperationalError("projection read unavailable")

    def unavailable(self, task_id):
        raise failure

    with Recorder(source, sqlite_path=database) as recorder:
        recorder.append(later)
        recorder.append(earlier)
        with monkeypatch.context() as patch:
            patch.setattr(SqliteTraceStore, "query", unavailable)
            assert recorder.query("task-1") == [earlier, later]
            assert recorder.projection_error is failure
            newest = event("after-read-failure", seconds=3)
            recorder.append(newest)
            assert recorder.query("task-1") == [earlier, later, newest]
            assert recorder.query("another-task") == []
        assert recorder.all_events() == [later, earlier, newest]

    with Recorder(source, sqlite_path=database) as recorder:
        assert recorder.query("task-1") == [earlier, later, newest]


def test_projection_fallback_keeps_utc_tie_order_and_task_isolation(tmp_path, monkeypatch):
    source, database = tmp_path / "trace.jsonl", tmp_path / "trace.db"
    offset = timezone(timedelta(hours=8))
    naive = event("a-naive").model_copy(update={
        "t0": BASE.replace(tzinfo=None),
        "t1": (BASE + timedelta(microseconds=17)).replace(tzinfo=None),
    })
    aware = event("b-aware").model_copy(update={
        "t0": BASE.astimezone(offset),
        "t1": (BASE + timedelta(microseconds=17)).astimezone(offset),
    })
    late, other = event("late", seconds=1), event("other", task_id="task-2")

    def unavailable(self, task_id):
        raise sqlite3.OperationalError("read unavailable")

    with Recorder(source, sqlite_path=database) as recorder:
        for item in (late, aware, other, naive):
            recorder.append(item)
        expected = recorder.query("task-1")
        assert expected == [naive, aware, late]
        with monkeypatch.context() as patch:
            patch.setattr(SqliteTraceStore, "query", unavailable)
            assert recorder.query("task-1") == expected
            assert recorder.query("task-2") == [other]
            assert recorder.query("absent") == []
        restored = recorder.query("task-1")
        assert restored[0].t0.tzinfo is None
        assert restored[1].t0.utcoffset() == timedelta(hours=8)


def test_jsonl_cannot_also_be_the_sqlite_database(tmp_path):
    source = tmp_path / "trace.jsonl"
    first = event("first")
    source.write_text(first.model_dump_json() + "\n", encoding="utf-8")
    original = source.read_bytes()

    with pytest.raises(ValueError):
        Recorder(source, sqlite_path=source)

    assert source.read_bytes() == original
    assert read_jsonl(source) == [first]


def test_invalid_existing_jsonl_fails_without_modifying_source_and_releases_files(tmp_path):
    source, database = tmp_path / "trace.jsonl", tmp_path / "trace.db"
    source.write_text(event("first").model_dump_json() + "\nnot-json\n", encoding="utf-8")
    original = source.read_bytes()

    with pytest.raises(TraceImportError) as caught:
        Recorder(source, sqlite_path=database)
    assert caught.value.line_number == 2
    assert source.read_bytes() == original
    # Retain the exception/constructor traceback above: on Windows leaked file
    # handles would prevent these renames even while the failed object survives.
    moved_source = source.rename(tmp_path / "saved.jsonl")
    moved_database = database.rename(tmp_path / "saved.db")
    assert moved_source.read_bytes() == original
    with SqliteTraceStore(moved_database) as store:
        assert store.query("task-1") == [], "startup import must remain atomic"


def test_engine_startup_failure_releases_its_task_database(tmp_path):
    root = tmp_path / "failed-engine"
    root.mkdir()
    journal = root / "trace.jsonl"
    journal.write_text("not-json\n", encoding="utf-8")
    original = journal.read_bytes()

    with pytest.raises(TraceImportError) as caught:
        Engine(FakeLLM([]), settings=engine_settings(root))

    assert caught.value.line_number == 1
    assert journal.read_bytes() == original
    # Keeping the constructor exception alive makes a leaked Engine/Database
    # reachable; Windows would refuse the rename while its handle was open.
    moved = (root / "moumou.db").rename(root / "preserved-moumou.db")
    connection = sqlite3.connect(moved)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()
