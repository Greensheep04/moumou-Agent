"""Automatic trace ancestry and actor scopes."""

import asyncio

import pytest

from moumou.store import SqliteTraceStore
from moumou.trace import Recorder


def test_nested_spans_and_markers_persist_original_parent_and_actor(tmp_path):
    source, database = tmp_path / "trace.jsonl", tmp_path / "trace.db"
    with Recorder(source, sqlite_path=database) as recorder:
        with recorder.span(task_id="task", kind="task", name="execution", agent_id="dev") as outer:
            with recorder.span(task_id="task", kind="tool", name="read") as inner:
                marker = recorder.record(task_id="task", kind="memory", name="selected")
            sibling = recorder.record(task_id="task", kind="task", name="after_tool")
        root = recorder.record(task_id="other", kind="task", name="next")
    with SqliteTraceStore(database) as store:
        saved = {event.span_id: event for event in store.query("task")}
    assert saved[inner.span_id].parent_span == outer.span_id
    assert saved[marker.span_id].parent_span == inner.span_id
    assert saved[sibling.span_id].parent_span == outer.span_id
    assert {event.agent_id for event in saved.values()} == {"dev"}
    assert root.parent_span is None and root.agent_id is None


async def test_parallel_async_stacks_cannot_replace_each_others_parents(tmp_path):
    with Recorder(tmp_path / "trace.jsonl") as recorder:
        both_started = asyncio.Event()
        ready = set()

        async def branch(identity):
            with recorder.span(task_id=identity, kind="task", name="execution",
                               agent_id=identity) as parent:
                ready.add(identity)
                if len(ready) == 2:
                    both_started.set()
                await both_started.wait()
                with recorder.span(task_id=identity, kind="tool", name="child") as child:
                    await asyncio.sleep(0)
                    marker = recorder.record(task_id=identity, kind="memory", name="observed")
                return parent, child, marker

        left, right = await asyncio.gather(branch("left"), branch("right"))
        final = recorder.record(task_id="final", kind="task", name="outside")
    for parent, child, marker in (left, right):
        assert parent.parent_span is None
        assert child.parent_span == parent.span_id
        assert marker.parent_span == child.span_id
        assert child.agent_id == marker.agent_id == parent.agent_id
    assert final.parent_span is None


def test_other_recorder_and_other_task_do_not_inherit_an_actor(tmp_path):
    with Recorder(tmp_path / "a.jsonl") as first, Recorder(tmp_path / "b.jsonl") as second:
        with first.span(task_id="one", kind="task", name="outer", agent_id="private") as parent:
            independent = second.record(task_id="one", kind="task", name="separate")
            different = first.record(task_id="two", kind="task", name="nested_task")
            explicit = first.record(task_id="two", kind="task", name="explicit",
                                    agent_id="public", parent_span="declared_parent")
    assert independent.parent_span is None and independent.agent_id is None
    assert different.parent_span == parent.span_id and different.agent_id is None
    assert explicit.parent_span == explicit.payload["parent_span"] == "declared_parent"
    assert explicit.agent_id == explicit.payload["agent_id"] == "public"


@pytest.mark.parametrize("cancelled", [False, True])
def test_error_and_cancellation_reset_the_active_context(tmp_path, cancelled):
    with Recorder(tmp_path / "trace.jsonl") as recorder:
        exception = asyncio.CancelledError if cancelled else RuntimeError
        with pytest.raises(exception):
            with recorder.span(task_id="one", kind="task", name="broken"):
                raise exception()
        later = recorder.record(task_id="two", kind="task", name="after")
        failed = recorder.query("one")[0]
    assert later.parent_span is None
    assert failed.status == ("cancelled" if cancelled else "error")


def test_append_failure_cannot_leave_a_parent_in_the_context(tmp_path, monkeypatch):
    with Recorder(tmp_path / "trace.jsonl") as recorder:
        append = recorder.append

        def fail(event):
            raise OSError("journal unavailable")

        monkeypatch.setattr(recorder, "append", fail)
        with pytest.raises(OSError, match="journal unavailable"):
            with recorder.span(task_id="one", kind="task", name="unwritten"):
                pass
        monkeypatch.setattr(recorder, "append", append)
        later = recorder.record(task_id="two", kind="task", name="after")
    assert later.parent_span is None


def test_handled_failure_status_is_not_overwritten_as_ok(tmp_path):
    with Recorder(tmp_path / "trace.jsonl") as recorder:
        with recorder.span(task_id="one", kind="task", name="execution") as event:
            event.status = "error"
            event.payload["task_status"] = "FAILED"
        saved = recorder.query("one")[0]
    assert saved.status == "error" and saved.payload["task_status"] == "FAILED"
