"""The conclusion board: what one instance of a role tells the next."""

from __future__ import annotations

import pytest

from moumou.context import ConclusionBoard
from moumou.contracts import TaskNode, TaskResult
from moumou.orchestrator.state import Database


@pytest.fixture
def board(tmp_path):
    db = Database(tmp_path / "board.db")
    yield ConclusionBoard(db, max_per_scope=5)
    db.close()


def node(task_id="t1", role="coder", status="DONE", **kw) -> TaskNode:
    base = dict(
        id=task_id,
        project_id="p",
        conversation_id="c",
        goal="build the parser",
        role=role,
        status=status,
    )
    return TaskNode(**{**base, **kw})


def test_a_finished_task_leaves_a_note(board):
    board.write(node(artifacts=["src/parser.py"], result=TaskResult(summary="parser works")))

    notes = board.recent(board.scope("p", "c", "coder"))

    assert len(notes) == 1
    assert "t1" in notes[0]
    assert "DONE" in notes[0]
    assert "src/parser.py" in notes[0]


def test_a_failed_task_is_recorded_too(board):
    board.write(node(status="DEAD", error="tests kept failing"))

    notes = board.recent(board.scope("p", "c", "coder"))

    assert "DEAD" in notes[0]
    assert "tests kept failing" in notes[0]


def test_roles_do_not_see_each_others_notes(board):
    board.write(node(task_id="t1", role="coder"))
    board.write(node(task_id="t2", role="reviewer"))

    assert len(board.recent(board.scope("p", "c", "coder"))) == 1
    assert len(board.recent(board.scope("p", "c", "reviewer"))) == 1
    assert board.recent(board.scope("p", "other", "coder")) == []


def test_only_the_most_recent_entries_are_returned(board):
    for index in range(4):
        board.write(node(task_id=f"t{index}"))

    notes = board.recent(board.scope("p", "c", "coder"), limit=2)

    assert len(notes) == 2
    assert "t2" in notes[0] and "t3" in notes[1], "oldest first within the window"


def test_the_board_is_pruned_per_scope(board):
    for index in range(9):
        board.write(node(task_id=f"t{index}"))

    assert board.count(board.scope("p", "c", "coder")) == 5, "max_per_scope wins"
