"""Append-only human quality review of saved deliveries; no model or task rerun."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from moumou.contracts import HumanRequest, HumanResponse

_ID = re.compile(r"[0-9a-f]{32}")
_SHA = re.compile(r"[0-9a-f]{64}")
_TERMINAL = {"DONE", "FAILED", "DEAD", "CANCELLED"}


def _read(path):
    # Share the report reader's duplicate-key, non-finite and size guards.
    from moumou.eval.report import _read as read

    return read(path)


def _inside(root, relative):
    from moumou.eval.report import _inside as inside

    return inside(root, relative)


def _new_json(path, value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    with path.open("xb") as stream:
        stream.write(raw)


@contextmanager
def _publication_lock(root):
    """Serialize the final recheck/write only; never lock a waiting human port."""
    directory = root / "reviews"
    lock = directory / ".publish.lock"
    if (directory.is_symlink() or lock.is_symlink()
            or not lock.resolve().is_relative_to(root)):
        raise ValueError("quality-review publication lock escapes its review directory")
    token = uuid4().hex.encode("ascii")
    try:
        stream = lock.open("xb")
    except FileExistsError as exc:
        raise ValueError("another quality-review publication is in progress; retry explicitly") \
            from exc
    identity = None
    try:
        with stream:
            stream.write(token)
            stream.flush()
            created = lock.stat()
            identity = (created.st_dev, created.st_ino)
        yield
    finally:
        try:
            # A removed/replaced lock belongs to someone else, even after failure.
            current = lock.stat()
            if (not lock.is_symlink() and identity == (current.st_dev, current.st_ino)
                    and lock.read_bytes() == token):
                lock.unlink()
        except FileNotFoundError:
            pass


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("review timestamps must be timezone-aware ISO text")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("missing timezone")
    except ValueError as exc:
        raise ValueError("review timestamps must be timezone-aware ISO text") from exc
    return value


def review_binding(root, manifest, scenario, task, path):
    """Bind immutable delivery bytes and saved source identity, not mutable totals."""
    identity, phase = scenario.get("scenario_id"), task.get("phase")
    if (not isinstance(identity, str) or not re.fullmatch(r"scenario_[0-9]+", identity)
            or phase not in {"task_1", "task_2"}):
        raise ValueError("invalid saved review task identity")
    expected = _inside(root, f"{identity}/{phase}-result.json")
    if expected != Path(path).resolve():
        raise ValueError("review result path disagrees with task identity")
    hashes = scenario.get("source_sha256")
    key = phase.replace("_", "")
    if (not isinstance(hashes, dict) or not hashes or key not in hashes
            or any(not isinstance(value, str) or not _SHA.fullmatch(value)
                   for value in hashes.values())):
        raise ValueError("review requires the saved original source hashes")
    code = manifest.get("code")
    code_sha = code.get("source_sha256") if isinstance(code, dict) else None
    if not isinstance(code_sha, str) or not _SHA.fullmatch(code_sha):
        raise ValueError("review requires the saved run-start code identity")
    kind = manifest.get("dataset_kind", "instructor")
    if kind not in {"instructor", "supplementary"}:
        raise ValueError("review source kind is unknown")
    root_id = task.get("root_task_id")
    source = manifest.get("source")
    if not isinstance(root_id, str) or not root_id or not isinstance(source, str) or not source:
        raise ValueError("review requires saved task and source identities")
    from moumou.eval.session_evidence import validate_artifact_snapshot

    validate_artifact_snapshot(root / identity, task.get("artifact_snapshot"), task=task)
    return {
        "run_started_at": _stamp(manifest.get("started_at")),
        "run_code_sha256": code_sha, "dataset_kind": kind, "source": source,
        "scenario_id": identity, "phase": phase, "root_task_id": root_id,
        "source_sha256": hashes, "goal_source_sha256": hashes[key],
        "result_relative_path": f"{identity}/{phase}-result.json",
        "result_sha256": hashlib.sha256(expected.read_bytes()).hexdigest(),
        "artifact_snapshot": task.get("artifact_snapshot"),
        "artifact_boundary": ("saved_immutable_snapshot" if task.get("artifact_snapshot") else
                              "unsupported_historical_artifact_snapshot"),
    }


def _target(root, scenario_id, phase):
    from moumou.eval.report import build_report

    # Validate saved task/node evidence before issuing any quality request.
    build_report(root)
    manifests = [p for p in (root / "test.json", root / "midtest.json") if p.is_file()]
    if len(manifests) != 1:
        raise ValueError("review needs one unambiguous saved session manifest")
    manifest = _read(manifests[0])
    if (manifest.get("schema_version") != 1
            or manifest.get("kind") not in {"instructor_test", "instructor_midtest"}):
        raise ValueError("unsupported review session")
    source_scenarios = manifest.get("scenarios", [])
    if not isinstance(source_scenarios, list) or any(not isinstance(s, dict)
                                                   for s in source_scenarios):
        raise ValueError("saved review scenarios must be objects")
    scenarios = [s for s in source_scenarios if s.get("scenario_id") == scenario_id]
    if len(scenarios) != 1:
        raise ValueError("review scenario has no unique saved delivery")
    scenario = scenarios[0]
    matches = [t for t in scenario.get("tasks", []) if t.get("phase") == phase]
    if len(matches) != 1:
        raise ValueError("unstarted or undelivered tasks cannot be reviewed")
    path = _inside(root, f"{scenario_id}/{phase}-result.json")
    task = _read(path)
    if task.get("phase") != phase or task.get("root_task_id") != matches[0].get("root_task_id"):
        raise ValueError("saved review task disagrees with the session index")
    if task.get("root_status") not in _TERMINAL:
        raise ValueError("only a saved terminal task can receive quality review")
    if task.get("verified_goal_success") is not None:
        raise ValueError("the original task already records a quality outcome")
    return manifest, scenario, task, path


def _evidence(folder, relative):
    path = folder / relative
    if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
        raise ValueError("review evidence escapes its append-only review directory")
    return path


def _verified_record(folder, record, binding):
    review_id = record.get("review_id")
    if (record.get("schema_version") != 1 or record.get("kind") != "offline_quality_review"
            or not isinstance(review_id, str) or not _ID.fullmatch(review_id)
            or folder.name != review_id or record.get("binding") != binding):
        raise ValueError("review identity/source/result hash mismatch")
    _stamp(record.get("reviewed_at"))
    intent = _read(_evidence(folder, "intent.json"))
    if (intent.get("review_id") != review_id or intent.get("binding") != binding
            or intent.get("supersedes") != record.get("supersedes")
            or intent.get("human_mode") not in {"console", "files"}):
        raise ValueError("review does not match its original human request intent")
    request_id = intent.get("request_id")
    if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{12}", request_id):
        raise ValueError("invalid review human request id")
    request = _read(_evidence(folder, f"human/requests/{request_id}.json"))
    response = _read(_evidence(folder, f"human/consumed/{request_id}.json"))
    events_path = _evidence(folder, "human/human-events.jsonl")
    if events_path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("review human log exceeds its size bound")
    from moumou.eval.report import _unique_object

    def invalid_constant(value):
        raise ValueError(f"non-finite review event value: {value}")

    events = [json.loads(line, object_pairs_hook=_unique_object, parse_constant=invalid_constant)
              for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    human_phase = f"quality_review/{binding['scenario_id']}/{binding['phase']}"
    req = request.get("request", {})
    expected_payload = {"review_id": review_id, "binding": binding,
                        "supersedes": record.get("supersedes")}
    if (request.get("phase") != human_phase or req.get("id") != request_id
            or req.get("kind") != "review" or req.get("task_id") != binding["root_task_id"]
            or req.get("agent_id") != "offline-quality-review"
            or req.get("action") != "review_saved_delivery"
            or req.get("options") != ["approve", "reject"]
            or req.get("payload") != expected_payload):
        raise ValueError("review has no matching explicit quality-review request")
    parsed = HumanResponse.model_validate(response)
    if (set(response) != {"request_id", "decision", "text"}
            or parsed.request_id != request_id or parsed.decision not in {"approve", "reject"}
            or record.get("response") != response):
        raise ValueError("review has no matching explicit consumed human decision")
    if (len(events) != 2 or any(not isinstance(event, dict) for event in events)
            or events[0].get("event") != "human_request"
            or events[0].get("phase") != human_phase or events[0].get("request") != req
            or events[1].get("event") != "human_response"
            or events[1].get("phase") != human_phase
            or events[1].get("request_id") != request_id
            or events[1].get("response") != response):
        raise ValueError("review is inconsistent with its HumanIO event log")
    for event in events:
        _stamp(event.get("ts"))
    wait = events[1].get("wait_s")
    if type(wait) not in {float, int} or not math.isfinite(wait) or wait < 0:
        raise ValueError("review human waiting time must be finite and nonnegative")
    return {**record, "path": str(folder / "review.json"),
            "evidence_method": "recorded_explicit_human_port_decision", "human_wait_s": wait,
            "identity_limit": "record consistency, not cryptographic human authentication"}


def resolve_review(root, manifest, scenario, task, path):
    """Resolve one nonforking review chain; arbitrary decision JSON is insufficient."""
    directory = root / "reviews"
    if directory.is_symlink():
        raise ValueError("review storage cannot be a symlink")
    if not directory.exists():
        return None
    records = {}
    binding = None
    for candidate in sorted(directory.glob("*/review.json")):
        if candidate.parent.is_symlink() or candidate.is_symlink():
            raise ValueError("review records cannot be symlinks")
        record = _read(candidate)
        saved = record.get("binding")
        if not isinstance(saved, dict):
            raise ValueError("review binding is missing")
        if (saved.get("scenario_id"), saved.get("phase")) != (
                scenario["scenario_id"], task["phase"]):
            continue
        binding = binding or review_binding(root, manifest, scenario, task, path)
        verified = _verified_record(candidate.parent, record, binding)
        records[verified["review_id"]] = verified
    if not records:
        return None
    roots = [r for r in records.values() if r.get("supersedes") is None]
    if len(roots) != 1:
        raise ValueError("ambiguous independent quality reviews; explicit supersession is required")
    current, seen, lineage = roots[0], set(), []
    while current["review_id"] not in seen:
        seen.add(current["review_id"])
        lineage.append(current)
        children = [r for r in records.values() if r.get("supersedes") == current["review_id"]]
        if len(children) > 1:
            raise ValueError("quality review chain forks")
        if not children:
            if len(seen) != len(records):
                raise ValueError("quality review chain is disconnected or cyclic")
            return {**current, "history": lineage}
        current = children[0]
    raise ValueError("quality review chain is cyclic")


async def review_task(root, scenario_id, phase, *, human_mode="console", supersedes=None):
    """Ask a real HumanIO port and append evidence; never mutate execution results."""
    from moumou.eval.human import ConsoleHuman, FileHuman

    if human_mode not in {"console", "files"}:
        raise ValueError("review requires console or file-based human input")
    root = Path(root).resolve()
    manifest, scenario, task, path = _target(root, scenario_id, phase)
    previous = resolve_review(root, manifest, scenario, task, path)
    if (previous is None and supersedes is not None) or (
            previous is not None and supersedes != previous["review_id"]):
        raise ValueError("repeat review requires --supersedes with the current review id")
    binding = review_binding(root, manifest, scenario, task, path)
    review_id = uuid4().hex
    folder = _inside(root, f"reviews/{review_id}")
    folder.mkdir(parents=True, exist_ok=False)
    request = HumanRequest(
        task_id=task["root_task_id"], agent_id="offline-quality-review", kind="review",
        action="review_saved_delivery", options=["approve", "reject"],
        summary=f"Review saved {scenario_id} {phase} delivery",
        context=(
            f"Inspect {path}, the original task/source identified below, delivered files "
            "and dialogue before deciding. This records your quality judgment, not a machine "
            "test or an automatic pass. The original result and manifest remain unchanged. "
            f"Immutable artifact snapshot: {binding['artifact_snapshot']}. "
            "A result hash does not certify historical workspace bytes if no immutable "
            "artifact snapshot was saved; later workspace changes must not be called the "
            "original delivery."
        ),
        payload={"review_id": review_id, "binding": binding, "supersedes": supersedes},
    )
    _new_json(folder / "intent.json", {
        "review_id": review_id, "binding": binding, "supersedes": supersedes,
        "human_mode": human_mode, "request_id": request.id,
    })
    human = (ConsoleHuman if human_mode == "console" else FileHuman)(folder / "human")
    human.set_phase(f"quality_review/{scenario_id}/{phase}")
    print(f"Review {review_id}; human evidence: {folder / 'human'}", flush=True)
    response = await human.ask(request)
    fresh = _target(root, scenario_id, phase)
    if review_binding(root, *fresh) != binding:
        raise ValueError("saved delivery/source changed while the human was reviewing")
    record = {
        "schema_version": 1, "kind": "offline_quality_review", "review_id": review_id,
        "reviewed_at": datetime.now(UTC).isoformat(), "binding": binding,
        "supersedes": supersedes, "response": response.model_dump(mode="json"),
    }
    _verified_record(folder, record, binding)
    # Independent processes must recheck and append under one short exclusive lock.
    with _publication_lock(root):
        latest = resolve_review(root, manifest, scenario, task, path)
        if (latest or {}).get("review_id") != supersedes:
            raise ValueError("another review completed; explicitly reconsider the current review")
        _new_json(folder / "review.json", record)
    return folder / "review.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Record human quality review of a saved delivery")
    parser.add_argument("--run", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--phase", required=True, choices=("task_1", "task_2"))
    parser.add_argument("--human", choices=("console", "files"), default="console")
    parser.add_argument("--supersedes", help="explicit current review id for a new review")
    args = parser.parse_args(argv)
    try:
        path = asyncio.run(review_task(args.run, args.scenario, args.phase,
                                       human_mode=args.human, supersedes=args.supersedes))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"cannot record quality review: {exc}")
        return 2
    print(f"Saved append-only review: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
