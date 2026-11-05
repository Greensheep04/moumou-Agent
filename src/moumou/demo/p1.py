"""No-key P1 integration demo. Run: python -m moumou.demo.p1."""

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from moumou.adapters import FakeLLM, GitWorkspaces, ScriptedHuman
from moumou.contracts import Acceptance, ToolContext
from moumou.demo.skeleton import responder
from moumou.orchestrator import Engine, Settings
from moumou.tools import default_tools


async def demonstrate(root):
    root = Path(root).resolve() / uuid.uuid4().hex[:12]
    workspace = GitWorkspaces(root / "workspaces")
    settings = Settings(
        project_id="p1-demo",
        conversation_id="c",
        root=root,
        memory_enabled=False,
        role_review_enabled=False,
    )
    with Engine(
        FakeLLM(responder), workspace=workspace, human=ScriptedHuman(), settings=settings
    ) as engine:
        engine.submit(
            "Build a runnable script and README.",
            acceptance=[
                Acceptance(kind="file_exists", target="hello.py"),
                Acceptance(kind="file_contains", target="README.md", expect="hello"),
            ],
        )
        report = await engine.run()
        if report.done != 3 or report.dead:
            raise RuntimeError(f"demo did not complete: {report.model_dump()}")
        parent = workspace.allocate("p1-demo", "c", "planner@t1")
        tools = {t.name: t for t in default_tools()}
        executed = await tools["run_python"].run(
            {"path": "hello.py"}, ToolContext(agent_id="planner@t1", task_id="t1", workspace=parent)
        )
        if not executed.ok:
            raise RuntimeError(executed.error)
    a, b, target = [workspace.allocate("conflict-demo", "c", name) for name in ("a", "b", "parent")]
    (a / "same.txt").write_text("A", encoding="utf-8")
    (b / "same.txt").write_text("B", encoding="utf-8")
    first = workspace.integrate(a, target, ["same.txt"])
    conflict = workspace.integrate(b, target, ["same.txt"])
    if not first.ok or conflict.conflicts != ["same.txt"]:
        raise RuntimeError("conflict demonstration failed")
    summary = {
        "done": report.done,
        "dead": report.dead,
        "script_output": executed.stdout.strip(),
        "conflicts": conflict.conflicts,
        "conflict_rolled_back": (target / "same.txt").read_text() == "A",
        "trace": str(root / "trace.jsonl"),
        "workspace": str(parent),
    }
    (root / "p1-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(workspace.git(parent, "log", "--oneline", "--graph").stdout)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="runs/p1-demo")
    args = parser.parse_args()
    asyncio.run(demonstrate(args.root))


if __name__ == "__main__":
    main()
