"""Behavioral contracts for the SQLite trace store and JSONL importer."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from moumou.contracts import TraceEvent, TraceStore
from moumou.store import (
    SqliteTraceStore,
    TraceConflictError,
    TraceImportError,
    TraceImportResult,
)
from moumou.trace import Recorder

BASE = datetime(2026, 11, 7, tzinfo=UTC)


def event(span_id: str = "span-1", task_id: str = "task-1", **changes) -> TraceEvent:
    fields = {
        "span_id": span_id,
        "task_id": task_id,
        "kind": "tool",
        "name": "read_file",
        "t0": BASE,
    }
    return TraceEvent(**{**fields, **changes})


def jsonl(path: Path, *records: TraceEvent) -> Path:
    path.write_text("".join(rec.model_dump_json() + "\n" for rec in records), encoding="utf-8")
    return path


@pytest.fixture
def store(tmp_path):
    with SqliteTraceStore(tmp_path / "trace.db") as trace_store:
        yield trace_store


def test_implements_trace_store_and_preserves_all_fields(store):
    item = event(
        parent_span="parent-1",
        agent_id="coder@task-1",
        kind="llm",
        name="中文模型请求",
        t1=BASE + timedelta(seconds=2, microseconds=13),
        status="cancelled",
        tokens_in=123,
        tokens_out=45,
        payload={"提示": "检查边界条件", "result": {"items": [1, None, True], "empty": []}},
    )
    assert isinstance(store, TraceStore)

    store.append(item)

    restored = store.query(item.task_id)
    assert len(restored) == 1
    assert restored[0].model_dump() == item.model_dump()
    assert restored[0].duration_ms == item.duration_ms


def test_query_only_matches_exact_task_ids(store):
    first = event("first", "task")
    second = event("second", "task-extra")
    quoted = event("quoted", "task' OR 1=1 --")
    for item in (first, second, quoted):
        store.append(item)

    assert store.query("task") == [first]
    assert store.query("task-extra") == [second]
    assert store.query("task' OR 1=1 --") == [quoted]
    assert store.query("missing") == []


def test_committed_events_survive_close_and_reopen(tmp_path):
    database = tmp_path / "nested" / "trace.db"
    item = event(payload={"message": "重启后仍然存在"})
    with SqliteTraceStore(str(database)) as writer:
        writer.append(item)

    with SqliteTraceStore(database) as reader:
        assert reader.query(item.task_id) == [item]


def test_query_orders_out_of_order_events_by_start_time(store):
    late = event("late", t0=BASE + timedelta(seconds=20))
    early = event("early", t0=BASE)
    middle = event("middle", t0=BASE + timedelta(seconds=10))
    for item in (late, early, middle):
        store.append(item)

    assert store.query("task-1") == [early, middle, late]


def test_tied_start_times_have_stable_span_id_order(store, tmp_path):
    database = tmp_path / "ties.db"
    first = event("a")
    second = event("b")
    with SqliteTraceStore(database) as writer:
        writer.append(second)
        writer.append(first)
        assert writer.query("task-1") == [first, second]

    with SqliteTraceStore(database) as reader:
        assert reader.query("task-1") == [first, second]


def test_offsets_are_sorted_by_their_utc_instant(store):
    earlier = event("earlier", t0=datetime(2026, 11, 7, 9, tzinfo=timezone(timedelta(hours=8))))
    later = event("later", t0=datetime(2026, 11, 7, 2, tzinfo=UTC))
    store.append(later)
    store.append(earlier)

    restored = store.query("task-1")
    assert restored == [earlier, later]
    assert restored[0].t0.utcoffset() == timedelta(hours=8)


def test_naive_start_times_sort_as_utc_without_changing_values(store):
    aware = event("aware", t0=datetime(2026, 11, 7, 2, tzinfo=UTC))
    naive = event("naive", t0=datetime(2026, 11, 7, 1))
    store.append(aware)
    store.append(naive)

    restored = store.query("task-1")
    assert restored == [naive, aware]
    assert restored[0].t0.tzinfo is None


def test_microsecond_order_survives_far_from_epoch(store):
    start = datetime(2400, 1, 1, tzinfo=UTC)
    first = event("z-first", t0=start + timedelta(microseconds=1))
    second = event("a-second", t0=start + timedelta(microseconds=2))
    for item in (second, first):
        store.append(item)

    assert store.query("task-1") == [first, second]


def test_append_takes_a_snapshot(store):
    item = event(payload={"nested": {"value": 1}})
    original = item.model_copy(deep=True)
    store.append(item)

    item.name = "mutated"
    item.payload["nested"]["value"] = 99

    assert store.query("task-1") == [original]


def test_duplicate_spans_are_idempotent_even_with_reordered_payload_keys(store):
    first = event(payload={"a": 1, "b": {"x": 2, "y": 3}})
    equivalent = event(payload={"b": {"y": 3, "x": 2}, "a": 1})
    store.append(first)
    store.append(equivalent)
    store.append(first)

    assert store.query("task-1") == [first]


@pytest.mark.parametrize(
    "changes",
    [{"task_id": "another-task"}, {"payload": {"different": True}}],
    ids=["different-task", "different-content"],
)
def test_conflicting_span_ids_never_replace_existing_records(store, changes):
    original = event(payload={"original": True})
    store.append(original)

    with pytest.raises(TraceConflictError):
        store.append(original.model_copy(update=changes))

    assert store.query("task-1") == [original]
    assert store.query("another-task") == []


def test_jsonl_import_preserves_records_and_source_file(store, tmp_path):
    first = event("one", payload={"text": "中文与空白"})
    second = event("two", "task-2", t1=BASE, status="ok", tokens_in=4)
    source = tmp_path / "events.jsonl"
    source.write_text(
        "\n" + first.model_dump_json() + "\n \t\n" + second.model_dump_json(),
        encoding="utf-8",
    )
    before = source.read_bytes()

    result = store.import_jsonl(source)

    assert result == TraceImportResult(read=2, inserted=2, skipped=0)
    assert store.query("task-1") == [first]
    assert store.query("task-2") == [second]
    assert source.read_bytes() == before


def test_repeated_jsonl_import_reports_inserted_and_skipped(store, tmp_path):
    old = event("old")
    new = event("new", t0=BASE + timedelta(seconds=1))
    store.append(old)
    source = jsonl(tmp_path / "repeat.jsonl", old, new, new)

    first = store.import_jsonl(source)
    second = store.import_jsonl(str(source))

    assert first == TraceImportResult(read=3, inserted=1, skipped=2)
    assert second == TraceImportResult(read=3, inserted=0, skipped=3)
    assert store.query("task-1") == [old, new]


def test_jsonl_import_accepts_utf8_bom_and_empty_lines(store, tmp_path):
    item = event()
    source = tmp_path / "bom.jsonl"
    source.write_bytes(b"\xef\xbb\xbf\n\n" + item.model_dump_json().encode("utf-8") + b"\n\n")

    result = store.import_jsonl(source)

    assert result == TraceImportResult(read=1, inserted=1, skipped=0)
    assert store.query("task-1") == [item]


@pytest.mark.parametrize(
    "bad_line",
    [b"{", b'{"span_id": "missing-required-fields"}', b"\xff"],
    ids=["malformed-json", "invalid-event", "invalid-utf8"],
)
def test_invalid_jsonl_reports_line_and_rolls_back_new_rows(store, tmp_path, bad_line):
    existing = event("existing", "old-task")
    incoming = event("incoming", "new-task")
    store.append(existing)
    source = tmp_path / "invalid.jsonl"
    source.write_bytes(b"\n" + incoming.model_dump_json().encode("utf-8") + b"\n\n" + bad_line)

    with pytest.raises(TraceImportError) as raised:
        store.import_jsonl(source)

    assert raised.value.source_path == source
    assert raised.value.line_number == 4
    assert str(source) in str(raised.value)
    assert "4" in str(raised.value)
    assert store.query("old-task") == [existing]
    assert store.query("new-task") == []


def test_import_conflict_reports_line_and_rolls_back_new_rows(store, tmp_path):
    existing = event("existing", payload={"value": 1})
    incoming = event("incoming", "new-task")
    conflicting = existing.model_copy(update={"payload": {"value": 2}})
    store.append(existing)
    source = jsonl(tmp_path / "conflict.jsonl", incoming, conflicting)

    with pytest.raises(TraceImportError) as raised:
        store.import_jsonl(source)

    assert raised.value.source_path == source
    assert raised.value.line_number == 2
    assert store.query("task-1") == [existing]
    assert store.query("new-task") == []


def test_intra_import_conflict_leaves_no_partial_rows(store, tmp_path):
    first = event("same", payload={"value": 1})
    conflicting = first.model_copy(update={"payload": {"value": 2}})
    source = jsonl(tmp_path / "duplicate-conflict.jsonl", first, conflicting)

    with pytest.raises(TraceImportError) as raised:
        store.import_jsonl(source)

    assert raised.value.line_number == 2
    assert store.query("task-1") == []


def test_real_recorder_jsonl_import_is_sorted_and_keeps_parent_links(store, tmp_path):
    source = tmp_path / "trace.jsonl"
    with Recorder(source) as recorder:
        with recorder.span(task_id="task", kind="task", name="parent") as parent:
            parent.t0 = BASE
            with recorder.span(task_id="task", kind="tool", name="child") as child:
                child.t0 = BASE + timedelta(seconds=1)
                child.parent_span = parent.span_id
                child.payload["text"] = "保留调用关系"
    original = source.read_bytes()
    source_records = [
        TraceEvent.model_validate_json(line) for line in original.decode("utf-8").splitlines()
    ]
    assert [item.name for item in source_records] == ["child", "parent"]

    result = store.import_jsonl(source)
    restored = store.query("task")

    assert result == TraceImportResult(read=2, inserted=2, skipped=0)
    assert [item.name for item in restored] == ["parent", "child"]
    assert restored[1].parent_span == restored[0].span_id
    assert {item.span_id: item for item in restored} == {
        item.span_id: item for item in source_records
    }
    assert source.read_bytes() == original


def test_independent_connections_see_committed_events(tmp_path):
    database = tmp_path / "shared.db"
    with SqliteTraceStore(database) as reader, SqliteTraceStore(database) as writer:
        assert reader.query("task-1") == []

        item = event()
        writer.append(item)

        assert reader.query("task-1") == [item]


def test_unknown_schema_version_is_not_modified(tmp_path):
    database = tmp_path / "future.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 999")

    with pytest.raises(ValueError, match="schema|version"):
        SqliteTraceStore(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 999


def test_new_databases_record_the_schema_version(tmp_path):
    database = tmp_path / "version.db"
    with SqliteTraceStore(database):
        pass

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_append_commit_survives_abrupt_process_exit(tmp_path):
    database = tmp_path / "abrupt.db"
    source_root = Path(__file__).resolve().parents[1] / "src"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source_root) + os.pathsep + environment.get("PYTHONPATH", "")
    script = (
        "import os, sys; "
        "from moumou.contracts import TraceEvent; "
        "from moumou.store import SqliteTraceStore; "
        "store = SqliteTraceStore(sys.argv[1]); "
        "store.append(TraceEvent(span_id='committed', task_id='abrupt', "
        "kind='tool', name='before_exit', payload={'saved': True})); "
        "os._exit(7)"
    )

    process = subprocess.run(
        [sys.executable, "-c", script, str(database)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert process.returncode == 7, process.stderr
    with SqliteTraceStore(database) as reader:
        restored = reader.query("abrupt")
    assert len(restored) == 1
    assert restored[0].span_id == "committed"
    assert restored[0].payload == {"saved": True}
