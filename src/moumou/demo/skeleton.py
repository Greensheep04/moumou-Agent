"""Walking skeleton — the whole framework in one screen.

Run it with:  python -m moumou.demo.skeleton

What it proves, without touching a real model:
    * the planner invents two roles and a 2-node DAG
    * both children run in parallel, each in its own workspace
    * executable acceptance checks decide DONE vs FAILED
    * the parent resumes after its children and reviews their reports
    * every step landed in trace.jsonl
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from moumou.adapters import FakeLLM, ScriptedHuman
from moumou.adapters.llm import LLMResponse, ToolCall
from moumou.contracts import Acceptance
from moumou.orchestrator import Engine, Settings

PLAN = {
    "reasoning": "Two independent deliverables; neither blocks the other.",
    "roles": [
        {
            "name": "python_dev",
            "title": "Python developer",
            "description": "Writes small, runnable Python programs.",
            "tools": ["write_file", "read_file", "run_python"],
            "reason": "no existing role can write code",
        },
        {
            "name": "tech_writer",
            "title": "Technical writer",
            "description": "Writes short, accurate README files.",
            "tools": ["write_file", "read_file"],
            "reason": "no existing role can write documentation",
        },
    ],
    "subtasks": [
        {
            "key": "code",
            "goal": "Create hello.py that prints exactly: hello, moumou",
            "role": "python_dev",
            "deps": [],
            "acceptance": [{"kind": "file_exists", "target": "hello.py"}],
        },
        {
            "key": "doc",
            "goal": "Create README.md with one line explaining how to run hello.py.",
            "role": "tech_writer",
            "deps": [],
            "acceptance": [
                {"kind": "file_contains", "target": "README.md", "expect": "hello"}
            ],
        },
    ],
}


def _steps(messages: list[dict]) -> int:
    return sum(1 for m in messages if m["role"] == "assistant")


def responder(messages: list[dict], tools) -> LLMResponse:
    system, brief = messages[0]["content"], messages[1]["content"]

    if "role: planner" in system:
        if "(this task has no upstream dependencies)" in brief:
            return LLMResponse(tool_calls=[ToolCall(id="p1", name="submit_plan", args=PLAN)])
        return LLMResponse(
            content=(
                "Both deliverables are complete: hello.py and README.md were produced by "
                "two specialists working in parallel, and both acceptance checks passed."
            )
        )

    if "role: python_dev" in system:
        if _steps(messages) == 0:
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="write_file",
                        args={"path": "hello.py", "content": "print('hello, moumou')\n"},
                    )
                ]
            )
        return LLMResponse(content="Wrote hello.py (3 lines).")

    if "role: tech_writer" in system:
        if _steps(messages) == 0:
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="w1",
                        name="write_file",
                        args={
                            "path": "README.md",
                            "content": "# Hello\n\nRun it with: `python3 hello.py`\n",
                        },
                    )
                ]
            )
        return LLMResponse(content="Wrote README.md.")

    return LLMResponse(content="(no scripted behaviour for this role)")


def _print_tree(tasks: list[dict]) -> None:
    by_parent: dict[str | None, list[dict]] = {}
    for t in tasks:
        by_parent.setdefault(t["parent_id"], []).append(t)

    def walk(parent: str | None, prefix: str = "") -> None:
        for item in sorted(by_parent.get(parent, []), key=lambda x: x["id"]):
            marker = "+-- " if prefix else ""
            print(
                f"{prefix}{marker}{item['id']:<10} [{item['status']:<5}] "
                f"{item['role']:<12} {item['goal'][:58]}"
            )
            walk(item["id"], prefix + ("|   " if prefix else "    "))

    walk(None)


async def main() -> None:
    root = Path("runs/demo")
    settings = Settings(project_id="demo", conversation_id="c1", root=root, max_slots=4)

    with Engine(FakeLLM(responder), human=ScriptedHuman(), settings=settings) as engine:
        engine.submit(
            "Build a tiny hello project: a runnable script and a README.",
            acceptance=[Acceptance(kind="file_contains", target="README.md", expect="hello")],
        )
        report = await engine.run()

        print("\n=== task tree ===")
        _print_tree(report.tasks)

        print("\n=== run report ===")
        print(
            f"total={report.total} done={report.done} dead={report.dead} "
            f"unfinished={report.unfinished}"
        )
        print(f"completion_rate={report.completion_rate}  wall={report.wall_s}s")
        print(f"tokens in/out = {report.tokens_in}/{report.tokens_out}")

        print("\n=== first 12 trace spans ===")
        for event in engine.trace.all_events()[:12]:
            print(
                f"  {event.t0.strftime('%H:%M:%S')} {event.kind:<10} "
                f"{event.name:<24} {event.status or '':<6} "
                f"{event.tokens_in + event.tokens_out:>5} tok"
            )
        print(f"\ntrace file: {(root / 'trace.jsonl').resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
