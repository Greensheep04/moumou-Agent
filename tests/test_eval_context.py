"""Current instructor goals stay complete when prior-task context exceeds its budget."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from moumou.context.tokens import TokenBudget
from moumou.contracts import Acceptance, ContextPolicy, TaskNode
from moumou.eval.cases import load_cases
from moumou.eval.team import ContextAdmissionError, TeamContextBuilder

ROOT = Path(__file__).resolve().parents[1]
# The actual 2,193-character delegated goal from the interrupted regression.
# This constant needs no live model, saved run, credentials or ignored evidence.
STATS_GOAL = (
    'Create dashboard_stats.py: a dependency-free module exposing compute_stats(db_path, start,'
    ' end) -> dict over the existing booking.py SQLite schema (tables '
    'rooms(id,name,capacity,active), bookings(id,room_id,title,start,end,cancelled,created); '
    'start/end are ISO-8601 text). Range is half-open [start,end). Definitions (document them '
    'in the module docstring): per-room booking_count = number of non-cancelled bookings '
    "overlapping the range; per-room reserved_minutes = sum of each such booking's overlap "
    'duration clipped to the range, in whole minutes; per-room cancelled_count = number of '
    'cancelled bookings overlapping the range; totals.cancellation_rate = total cancelled / '
    'total bookings (both overlapping the range), 0.0 when there are no bookings (no '
    'ZeroDivisionError). by_date = one entry per calendar date in the range with booking_count '
    "and reserved_minutes, clipping each booking's overlap to that date so a booking crossing "
    'midnight contributes to both dates. by_hour = 24 entries (hour 0..23) with booking_count '
    'and reserved_minutes, clipping overlap to each hour so a booking crossing an hour boundary'
    ' contributes to both hours. busiest_days and busiest_hours = the by_date/by_hour entries '
    'sorted descending by reserved_minutes (top 5). heavily_used_rooms = room names with '
    'reserved_minutes strictly greater than the mean over all rooms, sorted desc; '
    'underused_rooms = room names with reserved_minutes strictly less than the mean, sorted '
    'asc; both empty when all rooms are equal. Return a JSON-serialisable dict with keys: '
    'range, rooms (sorted by name), totals, by_date, by_hour, busiest_days, busiest_hours, '
    'heavily_used_rooms, underused_rooms. Also write test_stats.py with deterministic unit '
    'tests using a temporary SQLite fixture, covering: basic per-room counts and clipped '
    'minutes; a booking that crosses midnight (contributes to two by_date entries); a booking '
    'that crosses an hour boundary (contributes to two by_hour entries); a booking partially '
    'outside the range (clipped); cancelled bookings counted separately and excluded from '
    'booking_count/reserved_minutes; cancellation_rate arithmetic; and an empty/zero-booking '
    'range returning cancellation_rate 0.0 without error.'
)
SCHEMA_TAIL = (
    "range", "rooms", "totals", "by_date", "by_hour", "busiest_days", "busiest_hours",
    "heavily_used_rooms", "underused_rooms", "cancellation_rate",
)


def prior_evidence():
    raw = "actual archived dialogue, retained outside the opening prompt\n" * 600
    return {
        "previous_root_id": "t1",
        "previous_status": "DONE",
        "previous_goal": "Previously delivered booking CLI",
        "previous_result": {"summary": "Saved first delivery", "blockers": []},
        "dialogue_archive": {
            "role_private_path": ".evaluation/prior-dialogue.txt",
            "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "bytes": len(raw.encode("utf-8")),
        },
        "actual_human_exchanges": [{
            "request": {"id": "test-history", "kind": "approval", "summary": raw},
            "response": {"request_id": "test-history", "decision": "approve", "text": raw},
        }],
    }


def current_goal(kind, case):
    if kind == "teacher":
        assert len(case.task2) == 501
        return case.task2
    if kind == "stats":
        assert len(STATS_GOAL) == 2193
        return STATS_GOAL
    return STATS_GOAL + "\n\nMandatory final schema (preserve these line breaks):\n" + "\n".join(
        f"- {key}" for key in SCHEMA_TAIL
    ) + "\nEND_OF_CURRENT_REQUIREMENTS"


def node_for(goal, role):
    return TaskNode(
        id="t2.stats" if role == "dev" else "t2", project_id="scenario_1",
        conversation_id="instructor-group", goal=goal, role=role,
        acceptance=[
            Acceptance(kind="pytest", target="test_stats.py", timeout_s=73,
                       description="All current schema boundaries must be checked"),
            Acceptance(kind="file_contains", target="dashboard_stats.py",
                       expect="MANDATORY_TAIL_VERIFICATION", timeout_s=41,
                       description="Preserve the final acceptance detail too"),
        ],
    )


def task_section(brief):
    return brief.split("## Task\n", 1)[1].split("\n\n## Acceptance criteria", 1)[0]


@pytest.mark.parametrize("opening_budget", [8000, 4500], ids=["default", "constrained"])
@pytest.mark.parametrize("goal_kind", ["teacher", "stats", "multiline"])
def test_long_prior_context_cannot_displace_current_goal_or_acceptance(opening_budget, goal_kind):
    case = load_cases(ROOT / "eval/tasks")[0]
    role = case.roles[0] if goal_kind == "teacher" else case.roles[1]
    node = node_for(current_goal(goal_kind, case), role.name)
    policy = ContextPolicy(opening_budget_tokens=opening_budget)
    budget = TokenBudget()
    builder = TeamContextBuilder(policy, budget, scenario=case)
    builder.prior_context = prior_evidence()
    before_node = node.model_dump(mode="json")
    before_prior = copy.deepcopy(builder.prior_context)
    before_roles = [item.model_dump(mode="json") for item in case.roles]

    context = builder.build(node, role)
    messages = context.messages()
    assert context.pinned == 2
    assert [message["role"] for message in messages] == ["system", "user"]
    system, brief = [message["content"] for message in messages]
    assert task_section(brief) == node.goal
    assert role.description in system
    assert all(member.name in system and member.title in system for member in case.roles)
    assert budget.estimate(messages) <= min(policy.opening_budget_tokens, policy.prompt_ceiling)
    criteria_text = brief.split("## Acceptance criteria", 1)[1].split(
        "## Upstream work", 1,
    )[0]
    rendered_criteria = [
        json.loads(line[2:]) for line in criteria_text.splitlines() if line.startswith("- {")
    ]
    assert rendered_criteria == [criterion.model_dump(mode="json") for criterion in node.acceptance]
    if goal_kind != "teacher":
        assert all(key in task_section(brief) for key in SCHEMA_TAIL)
    if goal_kind == "multiline":
        assert task_section(brief).endswith("\nEND_OF_CURRENT_REQUIREMENTS")
    assert "truncated" in brief.lower()
    assert before_prior["dialogue_archive"]["role_private_path"] in brief
    assert before_prior["dialogue_archive"]["sha256"] in brief
    assert before_prior["actual_human_exchanges"][0]["request"]["summary"] not in brief
    assert node.model_dump(mode="json") == before_node
    assert builder.prior_context == before_prior
    assert [item.model_dump(mode="json") for item in case.roles] == before_roles


@pytest.mark.parametrize("opening_budget", [500, 8000])
def test_unfit_mandatory_goal_fails_explicitly_without_slicing_or_mutating_it(opening_budget):
    case = load_cases(ROOT / "eval/tasks")[0]
    role = case.roles[1]
    node = node_for((STATS_GOAL + "\n") * 30 + "MANDATORY_FINAL_SENTINEL", role.name)
    builder = TeamContextBuilder(
        ContextPolicy(opening_budget_tokens=opening_budget), TokenBudget(), scenario=case,
    )
    builder.prior_context = prior_evidence()
    before_node = node.model_dump(mode="json")
    before_prior = copy.deepcopy(builder.prior_context)
    with pytest.raises(ContextAdmissionError, match=r"(?i)mandatory.*opening budget"):
        builder.build(node, role)
    assert node.model_dump(mode="json") == before_node
    assert builder.prior_context == before_prior


def archive_builder(workspace):
    case = load_cases(ROOT / "eval/tasks")[0]
    role = case.roles[1]
    node = node_for(STATS_GOAL, role.name)
    builder = TeamContextBuilder(
        ContextPolicy(opening_budget_tokens=4500), TokenBudget(), scenario=case,
        workspaces={role.name: workspace},
    )
    builder.prior_context = prior_evidence()
    return builder, node, role


def test_deep_context_archive_preserves_full_goal_bytes_and_hash_without_long_names(tmp_path):
    # Match the real failing geometry: directory206 + slash + old64hex + .json =276.
    # The directory remains below Windows' legacy248-character creation bound.
    base = tmp_path.resolve()
    padding = 186 - len(str(base)) - 1
    assert 0 < padding < 255, "temporary base is too long for this controlled path fixture"
    workspace = base / ("w" * padding)
    workspace.mkdir()
    builder, node, role = archive_builder(workspace)
    before = copy.deepcopy(builder.prior_context)
    messages = builder.build(node, role).messages()
    brief = messages[1]["content"]
    archive, = (workspace / ".evaluation/context").glob("*.json")
    raw = archive.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    assert len(str(archive.parent)) == 206 < 248
    assert len(str(archive.parent / (digest + ".json"))) == 276 > 260
    assert len(str(archive)) == 244 < 260
    assert archive.name == digest[:32] + ".json"
    assert digest in brief and archive.relative_to(workspace).as_posix() in brief
    saved = json.loads(raw)
    assert saved["current_goal"] == node.goal == STATS_GOAL
    assert saved["acceptance"] == [item.model_dump(mode="json") for item in node.acceptance]
    assert saved["prior_context"] == before == builder.prior_context
    assert task_section(brief) == STATS_GOAL
    assert all(key in task_section(brief) for key in SCHEMA_TAIL)
    assert builder.budget.estimate(messages) <= 4500


def test_existing_short_archive_key_with_different_bytes_is_never_overwritten(tmp_path):
    workspace = tmp_path / "private"
    workspace.mkdir()
    builder, node, role = archive_builder(workspace)
    builder.build(node, role)
    archive, = (workspace / ".evaluation/context").glob("*.json")
    original = archive.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    assert archive.name == digest[:32] + ".json"
    collision = b"different preexisting content at the same short key\n"
    archive.write_bytes(collision)
    before_node = node.model_dump(mode="json")
    before_prior = copy.deepcopy(builder.prior_context)
    with pytest.raises(ContextAdmissionError, match="inconsistent"):
        builder.build(node, role)
    assert archive.read_bytes() == collision
    assert node.model_dump(mode="json") == before_node
    assert builder.prior_context == before_prior
