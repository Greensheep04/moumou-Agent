"""A resumed human callback must not grant the same search budget twice."""

import json
import sqlite3

import pytest

from moumou.eval.search_budget import SearchBudgetTool


def test_same_approval_is_idempotent_across_new_tool_instances(tmp_path):
    directory = tmp_path / "search"
    first = SearchBudgetTool(directory, max_requests=6, namespace="scenario_2")
    assert first.grant(2, approval_id="human-question")['limit'] == 8
    reopened = SearchBudgetTool(directory, max_requests=6, namespace="scenario_2")
    assert reopened.grant(2, approval_id="human-question")['limit'] == 8
    events = [json.loads(line) for line in reopened.events_path.read_text(encoding="utf-8").splitlines()]
    assert sum(row["event"] == "budget_granted" for row in events) == 1
    assert reopened.grant(1, approval_id="another-human-question")['limit'] == 9


def test_changed_amount_for_one_approval_is_rejected_without_a_quota_change(tmp_path):
    tool = SearchBudgetTool(tmp_path / "search", max_requests=6)
    tool.grant(2, approval_id="original")
    with pytest.raises(ValueError, match="conflicts"):
        tool.grant(3, approval_id="original")
    assert tool.stats()["limit"] == 8


def test_same_request_id_in_a_different_scenario_is_a_different_approval_scope(tmp_path):
    first = SearchBudgetTool(tmp_path / "search", max_requests=6, namespace="scenario_1")
    second = SearchBudgetTool(tmp_path / "search", max_requests=6, namespace="scenario_2")
    assert first.grant(1, approval_id="same-id")["limit"] == 7
    assert second.grant(2, approval_id="same-id")["limit"] == 8


def test_ambiguous_legacy_duplicate_grants_require_explicit_reconciliation(tmp_path):
    tool = SearchBudgetTool(tmp_path / "search", max_requests=6)
    tool.grant(2, approval_id="legacy-id")
    with sqlite3.connect(tool.database) as db:
        row = db.execute("SELECT namespace,kind,payload FROM events WHERE kind='budget_granted'").fetchone()
        db.execute("INSERT INTO events(namespace,kind,payload) VALUES(?,?,?)", row)
    with pytest.raises(ValueError, match="conflicts"):
        tool.grant(2, approval_id="legacy-id")
    assert tool.stats()["limit"] == 8
