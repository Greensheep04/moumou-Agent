"""Role revision: proposal, human decision, iteration, and its limits."""

from __future__ import annotations

import pytest

from moumou.adapters import FakeLLM, ScriptedHuman
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.contracts import HumanResponse, RoleSpec, TaskNode
from moumou.orchestrator.revision import RoleReviser
from moumou.orchestrator.state import Database, SqliteRoleRegistry

PROPOSAL = {
    "role": "coder",
    "proposed_description": "writes code and runs the tests before reporting",
    "reason": "kept reporting done without running anything",
    "evidence": ["t1.a"],
}


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "revision.db")
    roles = SqliteRoleRegistry(db)
    roles.create(
        RoleSpec(
            name="coder",
            title="Coder",
            description="writes code",
            project_id="p",
            created_by="model",
            reason="nothing existing writes code",
        )
    )
    yield roles, db
    db.close()


def a_task(status: str = "DONE", retries: int = 0, error: str | None = None) -> TaskNode:
    return TaskNode(
        id="t1.a",
        project_id="p",
        conversation_id="c",
        goal="write hello.py",
        role="coder",
        status=status,
        retries=retries,
        error=error,
    )


def llm_proposing(**overrides) -> FakeLLM:
    payload = {**PROPOSAL, **overrides}

    def responder(messages, tools):
        return LLMResponse(
            tool_calls=[ToolCall(id="r", name="submit_revision", args=payload)]
        )

    return FakeLLM(responder)


def human(*answers: tuple[str, str]) -> ScriptedHuman:
    seq = list(answers)

    def on_ask(request):
        decision, note = seq.pop(0)
        return HumanResponse(request_id=request.id, decision=decision, text=note)

    return ScriptedHuman(on_ask=on_ask)


async def test_an_approved_proposal_bumps_the_version(env):
    roles, _ = env
    scripted = human(("approve", "looks right"))
    reviser = RoleReviser(llm_proposing(), roles, scripted)

    updated = await reviser.review("p", [a_task()])

    assert updated is not None
    assert updated.version == 2
    assert "runs the tests" in roles.get("p", "coder").description
    assert len(scripted.requests) == 1

    log = roles.revision_log("p")
    assert len(log) == 1 and log[0]["decision"] == "approve"


async def test_a_rejection_without_a_note_ends_the_cycle(env):
    roles, _ = env
    scripted = human(("reject", ""))
    reviser = RoleReviser(llm_proposing(), roles, scripted)

    assert await reviser.review("p", [a_task()]) is None
    assert len(scripted.requests) == 1, "nothing to iterate on, so no second proposal"
    assert roles.get("p", "coder").version == 1


async def test_a_rejection_with_a_note_feeds_the_next_proposal(env):
    roles, _ = env
    scripted = human(("reject", "too broad, keep it to the testing step"), ("approve", ""))
    llm = llm_proposing()
    reviser = RoleReviser(llm, roles, scripted)

    updated = await reviser.review("p", [a_task()])

    assert updated is not None and updated.version == 2
    assert len(scripted.requests) == 2
    second_prompt = llm.calls[1][0]["content"]
    assert "too broad, keep it to the testing step" in second_prompt


async def test_it_gives_up_after_the_round_limit(env):
    roles, _ = env
    scripted = human(*[("reject", f"not yet {i}") for i in range(3)])
    reviser = RoleReviser(llm_proposing(), roles, scripted, max_rounds=3)

    assert await reviser.review("p", [a_task()]) is None
    assert len(scripted.requests) == 3
    assert roles.get("p", "coder").version == 1


async def test_a_noop_proposal_never_reaches_the_human(env):
    roles, _ = env
    scripted = human()
    reviser = RoleReviser(llm_proposing(role=""), roles, scripted)

    assert await reviser.review("p", [a_task()]) is None
    assert scripted.requests == []


async def test_an_identical_description_is_not_proposed(env):
    roles, _ = env
    scripted = human()
    reviser = RoleReviser(llm_proposing(proposed_description="writes code"), roles, scripted)

    assert await reviser.review("p", [a_task()]) is None
    assert scripted.requests == []


async def test_a_proposal_for_an_unknown_role_is_ignored(env):
    roles, _ = env
    scripted = human()
    reviser = RoleReviser(llm_proposing(role="ghost"), roles, scripted)

    assert await reviser.review("p", [a_task()]) is None
    assert scripted.requests == []


async def test_a_long_description_is_capped(env):
    roles, _ = env
    scripted = human(("approve", ""))
    reviser = RoleReviser(llm_proposing(proposed_description="x" * 5000), roles, scripted)

    updated = await reviser.review("p", [a_task()])

    assert updated is not None
    assert len(updated.description) == 800
