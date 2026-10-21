"""The two newer acceptance kinds, tested directly."""

from __future__ import annotations

from moumou.contracts import Acceptance, TaskNode
from moumou.orchestrator.acceptance import AcceptanceChecker


def node(label, *criteria) -> TaskNode:
    return TaskNode(
        id="t1",
        project_id="p",
        conversation_id="c",
        goal="g",
        role="r",
        acceptance=list(criteria),
    )


async def test_json_key_presence_and_equality(tmp_path):
    (tmp_path / "pkg.json").write_text('{"scripts": {"test": "pytest"}, "n": 3}')

    ok, _ = await AcceptanceChecker().check(
        node("a", Acceptance(kind="json_key", target="pkg.json", expect="scripts.test=pytest")),
        tmp_path,
    )
    assert ok

    ok, _ = await AcceptanceChecker().check(
        node("b", Acceptance(kind="json_key", target="pkg.json", expect="scripts.test")),
        tmp_path,
    )
    assert ok

    ok, detail = await AcceptanceChecker().check(
        node("c", Acceptance(kind="json_key", target="pkg.json", expect="scripts.test=unittest")),
        tmp_path,
    )
    assert not ok and detail[0]["value"] == "pytest"

    ok, _ = await AcceptanceChecker().check(
        node("d", Acceptance(kind="json_key", target="pkg.json", expect="scripts.missing")),
        tmp_path,
    )
    assert not ok


async def test_pytest_kind(tmp_path):
    (tmp_path / "test_sample.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")

    ok, detail = await AcceptanceChecker().check(
        node("a", Acceptance(kind="pytest", target=".", timeout_s=120)), tmp_path
    )
    assert ok, detail


async def test_empty_acceptance_is_recorded_as_self_report(tmp_path):
    ok, detail = await AcceptanceChecker().check(node("a"), tmp_path)
    assert ok
    assert detail[0]["kind"] == "self_report"
