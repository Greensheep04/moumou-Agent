"""Instructor scenarios: persistent four-member teams with real human input.

All formal task evaluation uses this protocol. Agent completion, machine
acceptance and human quality review are distinct.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit
from uuid import uuid4


class _TaskIncomplete(Exception):
    pass


class _QualityRejected(Exception):
    pass


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run the instructor's continuous team scenarios")
    parser.add_argument("--tasks", default="eval/tasks", help="instructor Markdown directory")
    parser.add_argument("--scenario", default="all", help="scenario id, or all in source order")
    parser.add_argument("--out", default=None, help="fresh suite directory under local runs")
    parser.add_argument("--human", choices=("console", "files"), default="console")
    parser.add_argument(
        "--quality-review",
        choices=("human", "deferred"),
        default="human",
        help="real post-delivery quality review; deferred never implies success",
    )
    parser.add_argument("--plan", action="store_true", help="inspect sources without API calls")
    parser.add_argument(
        "--search-requests",
        type=int,
        default=6,
        help="new Tavily requests per scenario, shared by both tasks",
    )
    parser.add_argument("--max-steps", type=int, default=80, help="per-node safety bound")
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=2_000_000,
        help="per-node cumulative safety bound, not a price ceiling",
    )
    parser.add_argument(
        "--task-timeout",
        type=int,
        default=3600,
        help="cooperative per-node seconds, including real human waiting",
    )
    return parser.parse_args(argv)


def _now():
    return datetime.now(UTC).isoformat()


def _dataset_kind(scenarios):
    kinds = {scenario.source_kind for scenario in scenarios}
    if len(kinds) != 1 or not kinds <= {"instructor", "supplementary"}:
        raise ValueError("one test suite must use a single declared evaluation-bank source")
    return next(iter(kinds))


def _tree(repo, root_id):
    tasks = repo.all_tasks()
    selected = {root_id}
    while True:
        following = selected | {node.id for node in tasks if node.parent_id in selected}
        if following == selected:
            return [node for node in tasks if node.id in selected]
        selected = following


def _task_record(engine, node, elapsed, phase):
    from moumou.eval.session_evidence import capture_failure_snapshot, capture_memory_snapshot

    nodes = _tree(engine.repo, node.id)
    current = engine.repo.get(node.id)
    verdicts = [
        event
        for event in engine.trace.query(node.id)
        if event.kind == "acceptance" and event.name == "verdict"
    ]
    has_criteria = bool(current.acceptance)
    machine_accepted = verdicts[-1].payload.get("ok") is True if has_criteria and verdicts else None
    return {
        "phase": phase,
        "root_task_id": current.id,
        "root_role": current.role,
        "root_status": current.status,
        "elapsed_s": round(elapsed, 6),
        "nodes": [
            {
                "id": item.id,
                "parent_id": item.parent_id,
                "project_id": item.project_id,
                "conversation_id": item.conversation_id,
                "role": item.role,
                "agent_id": item.role,
                "identity_policy": "instructor_fixed_role",
                "status": item.status,
                "deps": list(item.deps),
                "artifacts": list(item.artifacts),
                "error": item.error,
                "retries": item.retries,
                "wakeups": item.wakeups,
                "machine_criteria": len(item.acceptance),
                "result": item.result.model_dump(mode="json") if item.result else None,
            }
            for item in nodes
        ],
        "observed_subtasks": len(nodes) - 1,
        "observed_subtasks_done": sum(
            item.id != current.id and item.status == "DONE" for item in nodes
        ),
        "root_machine_criteria_present": has_criteria,
        "root_machine_accepted": machine_accepted,
        "agent_declared_complete": current.status == "DONE",
        "quality_status": "awaiting_human_or_independent_review",
        "verified_goal_success": None,
        "memory_snapshot": capture_memory_snapshot(engine, nodes),
        "failure_snapshot": capture_failure_snapshot(engine, nodes),
        "note": "DONE or empty-criteria self-report does not establish instructor task quality.",
    }


def _report(manifest):
    lines = [
        "# Test session",
        "",
        f"Status: {manifest['status']}",
        f"Dataset source: {manifest['dataset_kind']}",
        "Protocol: sequential team conversation scenarios.",
        "Agent completion, machine evidence and reviewed quality are reported separately.",
        "",
        "| scenario | phase | root | agent status | machine accepted | quality |",
        "|---|---|---|---|---|---|",
    ]
    for scenario in manifest["scenarios"]:
        for task in scenario.get("tasks", []):
            lines.append(
                f"| {scenario['scenario_id']} | {task['phase']} | "
                f"{task['root_task_id']} | {task['root_status']} | "
                f"{task['root_machine_accepted']} | {task['quality_status']} |"
            )
    lines += [
        "",
        "Full dialogue is in each scenario's dialogue.jsonl; actual human exchanges "
        "are also retained under human/. Task 2 uses the same team, experience and "
        "delivered files. Search cache hits are not fresh Tavily requests.",
        "Human wait spans are measured separately and can overlap; they are not "
        "subtracted blindly from end-to-end wall time.",
        "",
    ]
    return "\n".join(lines)


async def run_suite(args, scenarios):
    from moumou.adapters import DeepSeekClient
    from moumou.config import LLMSettings
    from moumou.contracts import Budget, HumanRequest
    from moumou.eval.artifacts import _code_version, atomic_json, atomic_text, redact
    from moumou.eval.human import ConsoleHuman, FileHuman, RecordingLLM
    from moumou.eval.search_budget import SearchBudgetTool
    from moumou.eval.session_evidence import (
        capture_artifact_snapshot,
        capture_scenario_state,
        sanitize_evidence,
    )
    from moumou.eval.team import configure_team, submit_teacher_task
    from moumou.orchestrator import Engine, Settings
    from moumou.tools import ToolRegistry, default_tools

    dataset_kind = _dataset_kind(scenarios)
    settings = LLMSettings.from_env()
    settings.require_key()
    root = (Path(args.out) if args.out else Path("runs/test") / uuid4().hex[:10]).resolve()
    if root.exists():
        raise ValueError("test needs a fresh output directory; existing sessions are preserved")
    root.mkdir(parents=True)
    secrets = tuple(value for value in (settings.api_key, os.getenv("TAVILY_API_KEY")) if value)
    manifest = {
        "schema_version": 1,
        "kind": "instructor_test",
        "dataset_kind": dataset_kind,
        "source": str(Path(args.tasks).resolve()),
        "status": "running",
        "started_at": _now(),
        "human_mode": args.human,
        "search_requests_per_scenario": args.search_requests,
        "planned_root_goals": sum(len(scenario.tasks) for scenario in scenarios),
        "scenarios": [],
        "code": _code_version(),
        "model": {
            "model": settings.model,
            "endpoint_host": urlsplit(settings.base_url).hostname,
            "timeout_s": settings.timeout_s,
            "max_retries": 2,
        },
        "environment": {"python": sys.version, "platform": platform.platform()},
        "task_budget": {
            "max_steps": args.max_steps,
            "max_tokens": args.max_tokens,
            "max_wall_s": args.task_timeout,
            "max_retries": 1,
        },
        "school_llm_policy": "quality first; token bound only guards runaway loops",
        "human_wait_in_node_budget": True,
        "automatic_quality_pass": False,
        "quality_review_mode": args.quality_review,
    }
    atomic_json(root / "test.json", manifest)
    budget = Budget(
        max_steps=args.max_steps,
        max_tokens=args.max_tokens,
        max_wall_s=args.task_timeout,
        max_retries=1,
    )
    print(f"test session: {root}", flush=True)
    unsuccessful_scenarios = []
    try:
        for scenario in scenarios:
            folder = root / scenario.id
            folder.mkdir()
            transcript = folder / "dialogue.jsonl"
            search = SearchBudgetTool(
                folder / "search", max_requests=args.search_requests, namespace=scenario.id
            )

            def accept_response(request, response, *, search_budget=search):
                command = re.fullmatch(
                    r"/grant-search\s+([1-9][0-9]*)", (response.text or "").strip()
                )
                if command and request.kind == "question":
                    search_budget.grant(int(command.group(1)), approval_id=request.id)

            human_class = ConsoleHuman if args.human == "console" else FileHuman
            human = human_class(
                folder / "human",
                secrets=secrets,
                transcript_path=transcript,
                on_response=accept_response,
            )
            llm = RecordingLLM(
                DeepSeekClient(
                    api_key=settings.api_key,
                    base_url=settings.base_url,
                    model=settings.model,
                    timeout_s=settings.timeout_s,
                    max_retries=2,
                ),
                transcript,
                secrets=secrets,
            )
            tools = ToolRegistry()
            tools.register_all(
                *(search if tool.name == "web_search" else tool for tool in default_tools())
            )
            try:
                engine = Engine(
                    llm,
                    human=human,
                    tools=tools,
                    settings=Settings(
                        root=folder,
                        project_id=scenario.id,
                        conversation_id="instructor-group",
                        memory_enabled=True,
                        max_slots=4,
                        role_review_enabled=False,
                        approval={"git_commit": True},
                    ),
                )
            except BaseException:
                await llm.aclose()
                raise
            entry = {
                "scenario_id": scenario.id,
                "name": scenario.name,
                "root": str(folder),
                "status": "running",
                "tasks": [],
                "source_sha256": scenario.source_sha256,
                "source_kind": scenario.source_kind,
                "roles": [role.model_dump(mode="json") for role in scenario.roles],
            }
            manifest["scenarios"].append(entry)
            previous = None
            scenario_ready = False
            try:
                team = configure_team(engine, scenario, previous_dialogue_path=transcript)
                team.context.search_budget_notice = (
                    f"This group initially has {args.search_requests} new Tavily requests "
                    "shared across both tasks and all four roles. Cache hits are separate. "
                    "The school model API is available for quality reasoning; this search "
                    "quota is not a token allowance. A human can grant more searches with "
                    "the explicit command /grant-search N.\n"
                )
                entry["workspaces"] = {role: str(path) for role, path in team.workspaces.items()}
                scenario_ready = True
                for index, goal in enumerate(scenario.tasks, start=1):
                    phase = f"task_{index}"
                    llm.set_phase(phase)
                    human.set_phase(phase)
                    atomic_json(
                        root / "active.json",
                        {
                            "scenario_id": scenario.id,
                            "phase": phase,
                            "scenario_root": str(folder),
                            "human_pending": str(folder / "human/pending"),
                            "manifest": str(root / "test.json"),
                        },
                    )
                    began = monotonic()
                    search_before = search.stats()
                    node = submit_teacher_task(
                        engine, scenario, goal, budget, previous_root=previous
                    )
                    atomic_json(root / "test.json", manifest)
                    await engine.run()
                    task = _task_record(engine, node, monotonic() - began, phase)
                    task["delivery_elapsed_s"] = task["elapsed_s"]
                    task["workspaces"] = entry["workspaces"]
                    capture_started = monotonic()
                    current = engine.repo.get(node.id)
                    task["artifact_snapshot"] = capture_artifact_snapshot(
                        folder, entry["workspaces"][current.role], current, phase,
                        secrets=secrets,
                    )
                    task["evidence_capture_s"] = round(monotonic() - capture_started, 6)
                    task = sanitize_evidence(task, secrets)
                    entry["tasks"].append(task)
                    atomic_json(folder / f"{phase}-result.json", task)
                    atomic_json(root / "test.json", manifest)
                    atomic_text(root / "report.md", _report(manifest))
                    if task["root_status"] == "DONE" and args.quality_review == "human":
                        request = HumanRequest(
                            task_id=node.id,
                            agent_id="instructor-session",
                            kind="review",
                            action="review_instructor_task",
                            summary=f"Review {scenario.name}, Task {index} deliverables",
                            context=(
                                "Inspect the actual output and dialogue before deciding. "
                                "Approval records your quality judgment; it does not create "
                                "a machine acceptance check. Rejection stops later tasks "
                                "in this group; independent groups can still proceed."
                            ),
                            options=["approve", "reject"],
                            payload={"scenario_root": str(folder), "task": task},
                        )
                        review_started = monotonic()
                        review = await human.ask(request)
                        task["quality_review_wait_s"] = round(monotonic() - review_started, 6)
                        task["quality_status"] = (
                            "human_accepted" if review.decision == "approve" else "human_rejected"
                        )
                        task["human_quality_response"] = review.model_dump(mode="json")
                        task["verified_goal_success"] = (
                            review.decision == "approve"
                            and task["root_machine_accepted"] is not False
                        )
                        task["quality_method"] = "actual_human_review"
                    task["total_to_review_s"] = round(monotonic() - began, 6)
                    task["llm"] = llm.summary()["by_phase"].get(phase, {})
                    task["human"] = human.summary()["by_phase"].get(phase, {})
                    search_after = search.stats()
                    task["search_scenario_cumulative"] = search_after
                    task["search_delta"] = {
                        key: search_after.get(key, 0) - search_before.get(key, 0)
                        for key in (
                            "tool_calls",
                            "provider_requests",
                            "cache_hits",
                            "deduplicated",
                            "budget_denials",
                        )
                    }
                    previous = node.id
                    task.update(sanitize_evidence(task, secrets))
                    atomic_json(folder / f"{phase}-result.json", task)
                    atomic_json(root / "test.json", manifest)
                    atomic_text(root / "report.md", _report(manifest))
                    print(
                        json.dumps(
                            {
                                "scenario": scenario.id,
                                "phase": phase,
                                "root_status": task["root_status"],
                                "quality_status": task["quality_status"],
                            }
                        ),
                        flush=True,
                    )
                    if task["root_status"] != "DONE":
                        entry["status"] = "incomplete"
                        raise _TaskIncomplete(
                            "instructor task did not finish; later tasks were not posted"
                        )
                    if task["quality_status"] == "human_rejected":
                        entry["status"] = "quality_rejected"
                        raise _QualityRejected("actual human review rejected the delivered task")
                entry["status"] = (
                    "human_accepted"
                    if args.quality_review == "human"
                    else "agent_completed_quality_unreviewed"
                )
            except (_TaskIncomplete, _QualityRejected) as exc:
                # Later tasks in this group depend on its earlier delivery.
                # Other instructor groups are independent and still need an attempt.
                entry["status"] = (
                    "quality_rejected" if isinstance(exc, _QualityRejected) else "incomplete"
                )
                entry["outcome_detail"] = str(exc)
                unsuccessful_scenarios.append(scenario.id)
            except Exception as exc:
                if not scenario_ready:
                    entry["status"] = "setup_error"
                    raise
                # Task-stage failure is local to this independent group. Setup
                # and cleanup errors remain infrastructure stops: resource
                # closure has not been established, so another group is unsafe.
                entry["status"] = "error"
                entry["error_type"] = type(exc).__name__
                entry["error_message"] = sanitize_evidence(redact(str(exc), settings), secrets)
                unsuccessful_scenarios.append(scenario.id)
            finally:
                entry["search"] = search.stats()
                entry["llm"] = llm.summary()
                entry["human"] = human.summary()
                try:
                    entry["state_snapshot"] = capture_scenario_state(
                        folder, engine, secrets=secrets,
                    )
                except Exception as exc:
                    entry["state_snapshot"] = {
                        "status": "unavailable", "error_type": type(exc).__name__,
                        "reason": "No state snapshot was established; do not infer final state.",
                    }
                try:
                    engine.close()
                finally:
                    await llm.aclose()
                atomic_json(root / "test.json", manifest)
        manifest["status"] = (
            "human_accepted" if args.quality_review == "human"
            else "agent_completed_quality_unreviewed"
        )
        if unsuccessful_scenarios:
            manifest["status"] = (
                "quality_rejected" if manifest["scenarios"][0]["status"] == "quality_rejected"
                else "error" if manifest["scenarios"][0]["status"] == "error"
                else "agent_incomplete"
            ) if len(scenarios) == 1 else "partially_completed"
            manifest["unsuccessful_scenarios"] = unsuccessful_scenarios
            return 1
    except (_TaskIncomplete, _QualityRejected) as exc:
        manifest["status"] = (
            "quality_rejected" if isinstance(exc, _QualityRejected) else "agent_incomplete"
        )
        manifest["outcome_detail"] = str(exc)
        return 1
    except BaseException as exc:
        manifest["status"] = (
            "interrupted"
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
            else "error"
        )
        manifest["error_type"] = type(exc).__name__
        manifest["error_message"] = sanitize_evidence(redact(str(exc), settings), secrets)
        raise
    finally:
        manifest["finished_at"] = _now()
        tasks = [task for scenario in manifest["scenarios"] for task in scenario["tasks"]]
        successes = sum(task["verified_goal_success"] is True for task in tasks)
        manifest["goal_metrics"] = {
            "planned_root_goals": manifest["planned_root_goals"],
            "persisted_task_deliveries": len(tasks),
            "agent_declared_complete": sum(task["agent_declared_complete"] for task in tasks),
            "reviewed_goals": sum(task["verified_goal_success"] is not None for task in tasks),
            "verified_successes": successes,
            "verified_goal_coverage": successes / manifest["planned_root_goals"],
            "quality_review_method": args.quality_review,
        }
        atomic_json(root / "test.json", manifest)
        atomic_text(root / "report.md", _report(manifest))
    return 0


def main(argv=None):
    args = parse_args(argv)
    if (
        args.search_requests < 0
        or args.max_steps < 1
        or args.max_tokens < 1000
        or args.task_timeout < 10
    ):
        print("invalid search or task safety bound", file=sys.stderr)
        return 2
    from moumou.eval.cases import load_cases

    try:
        scenarios = load_cases(Path(args.tasks))
        if args.scenario != "all":
            scenarios = [scenario for scenario in scenarios if scenario.id == args.scenario]
        if not scenarios:
            raise ValueError("the requested instructor scenario was not found")
        if args.plan:
            print(
                json.dumps(
                    {
                        "kind": "instructor_test",
                        "dataset_kind": _dataset_kind(scenarios),
                        "human": args.human,
                        "quality_review": args.quality_review,
                        "search_requests_per_scenario": args.search_requests,
                        "planned_root_goals": sum(len(case.tasks) for case in scenarios),
                        "scenarios": [
                            {
                                "id": case.id,
                                "name": case.name,
                                "roles": [role.name for role in case.roles],
                                "source_sha256": case.source_sha256,
                                "source_kind": case.source_kind,
                                "task_count": len(case.tasks),
                            }
                            for case in scenarios
                        ],
                    },
                    indent=2,
                )
            )
            return 0
        return asyncio.run(run_suite(args, scenarios))
    except (OSError, ValueError, RuntimeError) as exc:
        from moumou.eval.artifacts import redact

        print(f"cannot run instructor test: {redact(str(exc), None)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
