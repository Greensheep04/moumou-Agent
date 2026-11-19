"""Offline regression for explicit teacher opening quotas and mandatory context."""

from __future__ import annotations

import json

import pytest
from test_eval_team import engine, scenario
from test_quality_tools_cli import exercise, forbid, read, single_case

from moumou.adapters import FakeLLM
from moumou.config import LLMSettings
from moumou.context.builder import ContextAdmissionError
from moumou.context.tokens import estimate_messages
from moumou.contracts import Budget, ContextPolicy, HumanRequest, HumanResponse
from moumou.eval import cli
from moumou.eval import team as team_module
from moumou.eval.team import configure_team, submit_teacher_task, teacher_opening_policy


@pytest.mark.parametrize("value", ["0", "-1", "499", "1.5", str(2**31), "616001"])
def test_invalid_opening_cli_quota_precedes_keys_and_output(tmp_path, monkeypatch, value):
    monkeypatch.setattr(LLMSettings, "from_env", classmethod(forbid))
    monkeypatch.setattr("moumou.config.load_dotenv", forbid)
    output = tmp_path / "no-session"
    with pytest.raises(SystemExit) as error:
        cli.main(["--opening-context-tokens", value, "--out", str(output)])
    assert error.value.code == 2
    assert not output.exists()


@pytest.mark.parametrize("value", [True, False, 499, 32768.0, "32768", 2**31, 616001])
async def test_invalid_programmatic_quota_precedes_keys_and_output(
    tmp_path, monkeypatch, value,
):
    bank, case, _roles, _sha = single_case(tmp_path)
    output = tmp_path / "no-session"
    args = cli.parse_args(["--tasks", str(bank), "--out", str(output)])
    args.opening_context_tokens = value
    monkeypatch.setattr(LLMSettings, "from_env", classmethod(forbid))
    with pytest.raises(ValueError, match="opening context tokens"):
        await cli.run_suite(args, [case])
    assert not output.exists()


@pytest.mark.parametrize("requested,effective", [
    (None, 8000), (500, 500), (32768, 32768), (616000, 616000),
])
def test_plan_records_opening_policy_without_keys_calls_or_session(
    tmp_path, monkeypatch, capsys, requested, effective,
):
    bank, _case, _roles, _sha = single_case(tmp_path)
    output = tmp_path / "no-plan-session"
    monkeypatch.setattr(LLMSettings, "from_env", classmethod(forbid))
    monkeypatch.setattr("moumou.config.load_dotenv", forbid)
    monkeypatch.setattr("httpx.Client", forbid)
    monkeypatch.setattr("httpx.AsyncClient", forbid)
    options = ["--tasks", str(bank), "--out", str(output), "--plan"]
    if requested is not None:
        options += ["--opening-context-tokens", str(requested)]
    assert cli.main(options) == 0
    policy = json.loads(capsys.readouterr().out)["opening_context_policy"]
    assert policy["requested_tokens"] == requested
    assert policy["effective_tokens"] == effective
    assert policy["prompt_ceiling_tokens"] == 616000
    assert policy["token_estimator_calibration"] == "preserved"
    assert not output.exists()


@pytest.mark.parametrize("transport", ["buffered", "stream"])
async def test_formal_cli_uses_and_records_explicit_teacher_quota(
    tmp_path, monkeypatch, transport,
):
    original_parse = cli.parse_args
    original_configure = team_module.configure_team
    observed = []

    def with_quota(options):
        return original_parse([*options, "--opening-context-tokens", "32768"])

    def configured(run, case, **options):
        before = run.policy.model_dump()
        state = original_configure(run, case, **options)
        observed.append(state.context.policy.opening_budget_tokens)
        assert run.policy.model_dump() == before
        assert run.compactor.policy is run.policy
        assert state.context.policy is not run.policy
        assert state.context.budget is run.budget
        return state

    monkeypatch.setattr(cli, "parse_args", with_quota)
    monkeypatch.setattr(team_module, "configure_team", configured)
    output, _entry, constructors, _attempts = await exercise(
        tmp_path, monkeypatch, transport=transport,
    )
    assert observed == [32768]
    policy = read(output / "test.json")["opening_context_policy"]
    assert policy["requested_tokens"] == policy["effective_tokens"] == 32768
    assert policy["token_estimator_calibration"] == "preserved"
    assert "opening_budget_tokens" not in constructors[0]
    assert "opening_context_tokens" not in constructors[0]


async def test_unselected_cli_keeps_legacy_configure_signature_and_default_quota(
    tmp_path, monkeypatch,
):
    original_configure = team_module.configure_team
    observed = []

    # A default run must not pass the new keyword to existing integrations.
    def legacy_configured(run, case, previous_dialogue_path=None):
        state = original_configure(run, case, previous_dialogue_path)
        observed.append(state.context.policy)
        assert state.context.policy is run.policy
        return state

    monkeypatch.setattr(team_module, "configure_team", legacy_configured)
    output, _entry, _constructors, _attempts = await exercise(
        tmp_path, monkeypatch, transport="buffered",
    )
    assert [policy.opening_budget_tokens for policy in observed] == [8000]
    policy = read(output / "test.json")["opening_context_policy"]
    assert policy["requested_tokens"] is None
    assert policy["effective_tokens"] == 8000


def learn_from_usage(budget, calibration_messages, ratio):
    """Exercise real EMA updates; no resetting calibration or replacing its estimator."""
    for _ in range(12):
        estimated = budget.estimate(calibration_messages)
        measured = int(estimate_messages(calibration_messages) * ratio)
        budget.observe(estimated, measured)


@pytest.mark.parametrize("quota", [None, 32768])
def test_worker_opening_rebuild_preserves_required_facts_after_estimator_learning(tmp_path, quota):
    case = scenario(tmp_path)
    current_goal = "Complete current fixture requirement without omissions. " * 150
    current_goal += "CURRENT-GOAL-TAIL"
    original_goal = "Original ancestor fixture definitions remain applicable. " * 110
    original_goal += "ANCESTOR-GOAL-TAIL"
    answer = "Retain historical fixture records and their original unknowns. " * 10
    answer += "RECORDED-ANSWER-TAIL"
    with engine(tmp_path, FakeLLM([])) as run:
        original_policy = run.policy.model_dump()
        options = {} if quota is None else {"opening_budget_tokens": quota}
        configure_team(run, case, **options)
        parent = submit_teacher_task(run, case, original_goal, Budget(max_retries=0))
        run.repo.transition(parent.id, "RUNNING", "fixture_parent_started")
        parent = run.repo.get(parent.id)
        (child,) = run.delegation.apply(parent, {"subtasks": [{
            "key": "writer", "role": "dev", "goal": current_goal, "deps": [],
            "acceptance": [{"kind": "file_contains", "target": "result.md",
                            "expect": "ACCEPTANCE-SENTINEL"}],
        }]})
        request = HumanRequest(
            id="fixture-question", task_id=child.id, agent_id="dev", kind="question",
            summary="Which fixture records and qualifications must be preserved?",
            project_id=child.project_id, conversation_id=child.conversation_id, role=child.role,
        )
        run.repo.record_human_request(child, request)
        run.repo.record_human_response(child, HumanResponse(request_id=request.id, text=answer))
        role = run.roles.get(child.project_id, child.role)
        agent = run.runner._instance(child, role)
        calibration = [{"role": "user", "content": "Generic usage observation. " * 200}]
        learn_from_usage(run.budget, calibration, 0.5)
        low_correction = run.budget.correction

        def rebuild():
            return run.runner._opening(child, role, agent, {}, [], reason="fixture_rebuild")

        initial = rebuild().messages()
        assert current_goal in initial[1]["content"]
        assert original_goal in initial[1]["content"]
        assert answer in initial[1]["content"]
        assert run.budget.estimate(initial) <= 8000
        learn_from_usage(run.budget, calibration, 1.8)
        learned_correction = run.budget.correction
        assert learned_correction > low_correction
        assert run.budget.estimate(initial) > 8000
        if quota is None:
            with pytest.raises(ContextAdmissionError, match="mandatory.*opening budget"):
                rebuild()
        else:
            rebuilt = rebuild().messages()
            assert role.description in rebuilt[0]["content"]
            assert all(text in rebuilt[1]["content"] for text in (
                current_goal, original_goal, answer, "ACCEPTANCE-SENTINEL",
            ))
            assert run.budget.estimate(rebuilt) <= quota
        assert run.budget.correction == learned_correction
        assert run.policy.model_dump() == original_policy
        assert run.compactor.policy is run.policy
        assert run.repo.get(child.id).goal == current_goal
        assert run.repo.human_exchanges(child)[0]["response"]["text"] == answer
        assert not run.llm.calls


def test_actual_prompt_ceiling_is_kept_and_selected_policy_does_not_mutate_p1():
    original = ContextPolicy(context_window=30000, max_output_tokens=10000,
                             compact_trigger_tokens=18000, compact_target_tokens=9000)
    before = original.model_dump()
    assert teacher_opening_policy(policy=original) is original
    selected = teacher_opening_policy(16384, policy=original)
    assert selected.opening_budget_tokens == 16384
    assert selected.prompt_ceiling == original.prompt_ceiling == 20000
    assert teacher_opening_policy(20000, policy=original).opening_budget_tokens == 20000
    assert original.model_dump() == before
    with pytest.raises(ValueError, match="prompt ceiling"):
        teacher_opening_policy(32768, policy=original)


@pytest.mark.parametrize("quota", [True, 499, 616001])
def test_direct_teacher_configuration_rejects_before_roles_or_workspace_changes(tmp_path, quota):
    case = scenario(tmp_path)
    with engine(tmp_path, FakeLLM([])) as run:
        before = run.policy.model_dump()
        workspace = run.workspace
        context = run.context
        with pytest.raises(ValueError, match="opening context tokens"):
            configure_team(run, case, opening_budget_tokens=quota)
        assert run.policy.model_dump() == before
        assert run.workspace is workspace
        assert run.context is context
        assert not run.roles.list(run.settings.project_id)
        assert not run.repo.all_tasks()
        assert not run.llm.calls


def test_explicit_quota_still_refuses_unabridged_oversize_before_model(tmp_path):
    case = scenario(tmp_path)
    with engine(tmp_path, FakeLLM([])) as run:
        configure_team(run, case, opening_budget_tokens=32768)
        goal = "Every original fixture constraint must remain whole. " * 6000 + "GOAL-TAIL"
        node = submit_teacher_task(run, case, goal, Budget(max_retries=0))
        role = run.roles.get(node.project_id, node.role)
        agent = run.runner._instance(node, role)
        with pytest.raises(ContextAdmissionError, match="no truncated task was admitted"):
            run.runner._opening(node, role, agent, {}, [])
        assert run.repo.get(node.id).goal == goal
        assert run.policy.opening_budget_tokens == 8000
        assert not run.llm.calls
