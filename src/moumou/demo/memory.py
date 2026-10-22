"""A failure that teaches the next task.

Run it with:  python -m moumou.demo.memory

Task A cannot finish and dies. Reflection distils one lesson. Task B has a
similar goal, so the lesson is retrieved and lands in its system prompt -- and
this time the agent finishes. Nothing here touches a real model.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from moumou.adapters import FakeLLM
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.contracts import Acceptance
from moumou.orchestrator import Engine, Settings

LESSON = "依赖装不上时先换镜像源再重试"
REFLECTION_PREFIX = "You are reviewing a finished task"

GOAL_A = "安装依赖失败，修复它"
GOAL_B = "安装依赖并跑通测试"


def responder(messages: list[dict], tools) -> LLMResponse:
    # The reflection call comes in as a single user message.
    if len(messages) == 1 and messages[0]["content"].startswith(REFLECTION_PREFIX):
        return LLMResponse(
            tool_calls=[
                ToolCall(
                    id="lesson",
                    name="submit_lesson",
                    args={
                        "lessons": [
                            {
                                "situation": "installing dependencies fails",
                                "action": "switch to the mirror index before retrying",
                                "outcome": "failure",
                                "lesson": LESSON,
                            }
                        ]
                    },
                )
            ]
        )

    system = str(messages[0].get("content") or "")
    turns = sum(1 for m in messages if m.get("role") == "assistant")

    if LESSON in system and turns == 0:
        return LLMResponse(
            tool_calls=[
                ToolCall(
                    id="w",
                    name="write_file",
                    args={"path": "out.txt", "content": "installed via mirror\n"},
                )
            ]
        )
    if LESSON in system:
        return LLMResponse(content="Wrote out.txt after switching to the mirror.")
    return LLMResponse(content="I could not work out how to install the dependency.")


def print_tasks(report) -> None:
    for task in report.tasks:
        indent = "    " if task["parent_id"] else ""
        print(f"{indent}{task['id']:<8} [{task['status']:<5}] {task['goal'][:56]}")


def print_memories(engine, scope: str) -> None:
    records = engine.memory.all(scope)
    if not records:
        print("  (no lessons yet)")
        return
    for record in records:
        print(f"  - {record.lesson}")
        print(f"    score={record.score:.2f}  uses={record.uses}  from={record.task_id}")


async def main() -> None:
    root = Path("runs/demo-memory")
    settings = Settings(project_id="demo", conversation_id="c1", root=root)
    scope = "demo:planner"

    with Engine(FakeLLM(responder), settings=settings) as engine:
        print("=== run 1: no experience yet ===")
        engine.submit(GOAL_A, acceptance=[Acceptance(kind="file_exists", target="out.txt")])
        first = await engine.run()
        print_tasks(first)
        print(f"  completion={first.completion_rate}  dead={first.dead}")
        print("\n  lessons after reflection:")
        print_memories(engine, scope)

        print("\n=== run 2: same role, similar goal ===")
        engine.submit(GOAL_B, acceptance=[Acceptance(kind="file_exists", target="out.txt")])
        second = await engine.run()
        print_tasks(second)
        print(f"  completion={second.completion_rate}  done={second.done}")

        print("\n  lessons now:")
        print_memories(engine, scope)

        print("\n=== conclusion board (project:conversation:role) ===")
        for note in engine.board.recent(engine.board.scope("demo", "c1", "planner")):
            print(f"  - {note[:110]}")

        print("\n=== how the second run knew ===")
        injected = [c for c in engine.runner.citations.values()]
        print(f"  memories cited by tasks: {injected or 'none'}")
        print(f"  token-estimate correction learned: {engine.budget.correction:.3f}")
        print(f"\nartifacts under: {root.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
