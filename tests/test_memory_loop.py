"""End-to-end memory: a failure teaches the next run.

This is the test that makes "experience evolution" a claim rather than a feature
list: task A dies, reflection distils a lesson, task B is built with that lesson
already in its system prompt.
"""

from __future__ import annotations

import pytest

from moumou.adapters import FakeLLM
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.contracts import Acceptance
from moumou.orchestrator import Engine, Settings

LESSON = "依赖装不上时先换镜像源再重试"
REFLECTION_PREFIX = "You are reviewing a finished task"


def settings(tmp_path, **kw) -> Settings:
    return Settings(project_id="p", conversation_id="c", root=tmp_path / "run", **kw)


def learning_llm() -> FakeLLM:
    """Fails every task, but produces one lesson when asked to reflect."""

    def responder(messages, tools):
        if len(messages) == 1 and messages[0]["content"].startswith(REFLECTION_PREFIX):
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="lesson",
                        name="submit_lesson",
                        args={
                            "lessons": [
                                {
                                    "situation": "installing dependencies",
                                    "action": "switch to the mirror first",
                                    "outcome": "failure",
                                    "lesson": LESSON,
                                }
                            ]
                        },
                    )
                ]
            )
        return LLMResponse(content="I could not finish it")

    return FakeLLM(responder)


def task_prompts(llm: FakeLLM, needle: str) -> list[list[dict]]:
    return [
        call
        for call in llm.calls
        if len(call) >= 2 and needle in str(call[1].get("content", ""))
    ]


async def test_a_failed_task_teaches_the_next_one(tmp_path):
    llm = learning_llm()
    engine = Engine(llm, settings=settings(tmp_path))
    try:
        engine.submit(
            "安装依赖失败，修复它",
            acceptance=[Acceptance(kind="file_exists", target="out.txt")],
        )
        await engine.run()

        after_first = engine.memory.all("p:planner")
        assert len(after_first) == 1, "one reflection should have produced one lesson"
        assert after_first[0].lesson == LESSON
        assert after_first[0].score == pytest.approx(1.0), "a fresh lesson must not decay"

        engine.submit(
            "安装依赖并跑通测试",
            acceptance=[Acceptance(kind="file_exists", target="out.txt")],
        )
        await engine.run()
        after_second = engine.memory.all("p:planner")
    finally:
        engine.close()

    prompts = task_prompts(llm, "跑通测试")
    assert prompts, "the second task never ran"
    assert LESSON in prompts[0][0]["content"], "the lesson must reach the system prompt"
    assert len(after_second) == 1, "the duplicate lesson must be merged, not repeated"


async def test_a_recalled_lesson_that_fails_again_loses_score(tmp_path):
    llm = learning_llm()
    engine = Engine(llm, settings=settings(tmp_path))
    try:
        engine.submit(
            "安装依赖失败，修复它",
            acceptance=[Acceptance(kind="file_exists", target="out.txt")],
        )
        await engine.run()
        engine.submit(
            "安装依赖并跑通测试",
            acceptance=[Acceptance(kind="file_exists", target="out.txt")],
        )
        await engine.run()
        record = engine.memory.all("p:planner")[0]
    finally:
        engine.close()

    assert record.uses >= 1
    assert record.score == pytest.approx(0.7, abs=0.01), "1.0 - 0.3 for a failed reuse"


async def test_memory_can_be_switched_off(tmp_path):
    llm = learning_llm()
    engine = Engine(llm, settings=settings(tmp_path, memory_enabled=False))
    try:
        engine.submit(
            "安装依赖失败，修复它",
            acceptance=[Acceptance(kind="file_exists", target="out.txt")],
        )
        await engine.run()
        recorded = engine.memory.all("p:planner")
    finally:
        engine.close()

    assert recorded == [], "no reflection when memory is disabled"
    assert not any(LESSON in str(call[0].get("content", "")) for call in llm.calls)
