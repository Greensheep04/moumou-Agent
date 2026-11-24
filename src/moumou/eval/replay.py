"""Read-only, source-linked instructor evidence for a P2 replay consumer.

The export preserves saved states and original events. It does not run an Engine,
sample a live database, infer missing parent spans, or impersonate a human reply.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

from moumou.contracts import TraceEvent
from moumou.eval.report import _canonical, build_report

_LINE_LIMIT = 32 * 1024**2
_RAW_NAMES = {"test.json", "midtest.json", "active.json", "review.json", "intent.json"}
_NODE_STATUSES = {
    "PENDING", "READY", "RUNNING", "WAITING_HUMAN", "BLOCKED", "DONE", "FAILED", "DEAD",
}


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate replay evidence field: {key}")
        result[key] = value
    return result


def _invalid(value):
    raise ValueError(f"non-finite replay evidence value: {value}")


def _loads(value):
    return json.loads(value, object_pairs_hook=_pairs, parse_constant=_invalid)


def _inside(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError("replay evidence escapes its saved suite")
    return path


def _source(path, root):
    path = _inside(root, path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return {"path": str(path), "relative_path": path.relative_to(root).as_posix(),
            "sha256": digest.hexdigest(), "bytes": path.stat().st_size}


def _json(path, root):
    path = _inside(root, path)
    raw = path.read_bytes()
    if len(raw) > _LINE_LIMIT:
        raise ValueError("replay JSON evidence exceeds 32 MiB")
    value = _loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("replay JSON evidence must contain an object")
    source = _source(path, root)
    if source["sha256"] != hashlib.sha256(raw).hexdigest():
        raise ValueError("replay evidence changed while reading")
    return value, source


def _journal(path, root, *, selected=None):
    path = _inside(root, path)
    if not path.exists():
        return {"state": "missing", "path": str(path), "source": None, "events": []}
    source = _source(path, root)
    rows = []
    with path.open("rb") as stream:
        for line_number, raw in enumerate(stream, 1):
            if not raw.strip():
                continue
            if len(raw) > _LINE_LIMIT:
                raise ValueError("replay journal line exceeds 32 MiB")
            try:
                record = _loads(raw.decode("utf-8-sig"))
            except (ValueError, UnicodeError) as exc:
                raise ValueError(f"invalid replay journal at {path.name}:{line_number}") from exc
            if not isinstance(record, dict):
                raise ValueError("replay journal event must be an object")
            if selected is None or selected(record):
                rows.append({"record": record, "source": {
                    **source, "line_number": line_number,
                    "line_sha256": hashlib.sha256(raw).hexdigest(),
                }})
    if _source(path, root) != source:
        raise ValueError("replay journal changed while reading")
    return {"state": "available", "path": str(path), "source": source, "events": rows}


def _identity(scenario_id, task_id):
    return {"scenario_id": scenario_id, "task_id": task_id}


def _validate_trace(event):
    # Optional historical fields may be absent; never dump defaults into the export.
    required = {"span_id", "task_id", "kind", "name", "t0", "status"}
    if not required.issubset(event):
        raise ValueError("trace event is missing required identity/time/status fields")
    TraceEvent.model_validate_json(json.dumps(event, allow_nan=False), strict=True)
    for key in ("t0", "t1"):
        value = event.get(key)
        if key == "t1" and value is None:
            continue
        if not isinstance(value, str):
            raise ValueError("trace timestamps must be saved ISO strings")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("invalid saved trace timestamp") from exc
        if parsed.utcoffset() is None:
            raise ValueError("saved trace timestamps must include timezone")


def _human(folder, root, scenario_id):
    private = folder / "human" / "human-events.jsonl"
    # ConsoleHuman has only the shared transcript; FileHuman also has its own journal.
    path = private if private.exists() else folder / "dialogue.jsonl"
    journal = _journal(
        path, root, selected=lambda row: str(row.get("event", "")).startswith("human_"),
    )
    requests, answers, unmatched = {}, [], []
    for item in journal["events"]:
        record = item["record"]
        if record.get("event") == "human_request":
            request = record.get("request")
            if (not isinstance(request, dict) or not isinstance(request.get("id"), str)
                    or not request["id"]):
                raise ValueError("saved human request has no identity")
            if request["id"] in requests:
                raise ValueError("duplicate saved human request identity")
            requests[request["id"]] = item
        elif record.get("event") == "human_response":
            response = record.get("response")
            if not isinstance(response, dict):
                raise ValueError("saved human response must be an object")
            request_id = response.get("request_id") or record.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                raise ValueError("saved human response has no request identity")
            if (record.get("request_id") is not None
                    and record["request_id"] != request_id):
                raise ValueError("human response request identities disagree")
            request_event = requests.get(request_id)
            if request_event is None:
                unmatched.append(item)
                continue
            if any(pair["request_id"] == request_id for pair in answers):
                raise ValueError("duplicate saved human response identity")
            request = request_event["record"]["request"]
            answers.append({
                "request_id": request_id,
                "identity": _identity(scenario_id, request.get("task_id")),
                "request_event": request_event, "response_event": item,
                "provenance": "recorded HumanIO response; manual/controller origin not inferred",
            })
    answered = {item["request_id"] for item in answers}

    def files(directory):
        rows = []
        location = folder / "human" / directory
        if location.exists():
            _inside(root, location)
            for candidate in sorted(location.glob("*.json")):
                record, source = _json(candidate, root)
                request = record.get("request", record)
                task_id = request.get("task_id") if isinstance(request, dict) else None
                rows.append({"record": record, "source": source,
                             "identity": _identity(scenario_id, task_id)})
        return rows

    return {
        "journal": journal, "answered": answers,
        "unanswered_requests": [item for key, item in requests.items() if key not in answered],
        "unmatched_response_events": unmatched,
        "pending_files": files("pending"),
        "staged_response_files": files("responses"),
        "consumed_response_files": files("consumed"),
        "limits": [
            "A response file is not evidence that the running agent consumed it.",
            "Pending/unanswered evidence does not establish that a process is still alive.",
            "HumanIO records alone do not authenticate manual versus assisted authorship.",
        ],
    }


def _artifact_capture(folder, original, root):
    ref = original.get("artifact_snapshot")
    if ref is None:
        return {"state": "unknown", "reference": None, "manifest": None, "source": None}
    from moumou.eval.session_evidence import validate_artifact_snapshot

    manifest = validate_artifact_snapshot(folder, ref, task=original)
    if manifest is None:
        return {"state": "unknown", "reference": ref, "manifest": None, "source": None}
    return {"state": "validated_saved_capture", "reference": ref, "manifest": manifest,
            "source": _source(folder / ref["path"], root)}


def _scenario_state(folder, scenario, root):
    ref = scenario.get("state_snapshot")
    if ref is None:
        return {"state": "unknown", "reference": None, "record": None, "source": None}
    if not isinstance(ref, dict):
        raise ValueError("scenario state snapshot reference must be an object")
    if ref.get("status") == "unavailable":
        return {"state": "unavailable", "reference": ref, "record": None, "source": None}
    if (ref.get("schema_version") != 1 or ref.get("status") != "recorded"
            or ref.get("path") != "state-snapshot.json"
            or ref.get("kind") != "observed_state_not_delivery"):
        raise ValueError("unsupported scenario state snapshot reference")
    path = _inside(folder, folder / ref["path"])
    record, source = _json(path, root)
    if source["sha256"] != ref.get("sha256"):
        raise ValueError("scenario state snapshot hash mismatch")
    if (record.get("schema_version") != 1 or record.get("scenario_id") != scenario["scenario_id"]
            or record.get("kind") != "observed_state_not_delivery"
            or record.get("stage") != "scenario_close"
            or record.get("captured_at") != ref.get("captured_at")):
        raise ValueError("scenario state snapshot identity mismatch")
    conversation = record.get("conversation_id")
    nodes = record.get("nodes")
    if not isinstance(conversation, str) or not conversation or not isinstance(nodes, list):
        raise ValueError("scenario state snapshot lacks conversation or nodes")
    seen = set()
    for node in nodes:
        if (not isinstance(node, dict) or not isinstance(node.get("id"), str) or not node["id"]
                or node["id"] in seen or node.get("project_id") != scenario["scenario_id"]
                or node.get("conversation_id") != conversation
                or not isinstance(node.get("role"), str) or not node["role"]
                or not isinstance(node.get("status"), str)
                or node["status"] not in _NODE_STATUSES):
            raise ValueError("scenario state snapshot node identity mismatch")
        seen.add(node["id"])
    if type(ref.get("node_count")) is not int or ref["node_count"] != len(nodes):
        raise ValueError("scenario state snapshot node count mismatch")
    return {"state": "validated_saved_diagnostic", "reference": ref,
            "record": record, "source": source,
            "interpretation": "observed state only; not deliveries or a new goal plan"}


def _from_report(report):
    """Share one validated report view with analysis, including its review overlay."""
    if report["kind"] != "instructor_test":
        raise ValueError("replay export requires an instructor-format saved suite")
    root = Path(report["root"]).resolve()
    scenarios, tasks = [], []
    records = report["records"]
    for scenario in report["manifest"]["scenarios"]:
        identity = scenario["scenario_id"]
        folder = _inside(root, root / identity)
        trace = _journal(folder / "trace.jsonl", root)
        for item in trace["events"]:
            event = item["record"]
            _validate_trace(event)
            if not isinstance(event.get("task_id"), str) or not event["task_id"]:
                raise ValueError("trace event has no saved task identity")
            if (not isinstance(event.get("span_id"), str) or not event["span_id"]
                    or not isinstance(event.get("payload", {}), dict)):
                raise ValueError("trace event has invalid identity or payload")
            item["identity"] = _identity(identity, event["task_id"])
        saved_results = []
        for row in records:
            if row["scenario_id"] != identity:
                continue
            original, source = _json(row["path"], root)
            content_hash = hashlib.sha256(_canonical(original).encode("utf-8")).hexdigest()
            if content_hash != row.get("result_content_sha256"):
                raise ValueError("task result content changed after report validation")
            if (original.get("phase") != row["phase"]
                    or original.get("root_task_id") != row["root_task_id"]
                    or original.get("root_status") != row["root_status"]):
                raise ValueError("task evidence changed after report validation")
            saved_results.append({"record": original, "source": source,
                                  "artifact_snapshot": _artifact_capture(folder, original, root),
                                  "memory_snapshot": original.get("memory_snapshot"),
                                  "effective_quality": {
                                      key: row.get(key) for key in (
                                          "quality_status", "verified_goal_success",
                                          "quality_source", "raw_quality", "review",
                                      )}})
            for node in original["nodes"]:
                tasks.append({"identity": _identity(identity, node["id"]),
                              "phase": row["phase"], "root_task_id": row["root_task_id"],
                              "node": node, "source": source})
        scenarios.append({
            "scenario_id": identity, "saved_status": scenario.get("status"),
            "task_results": saved_results, "trace": trace,
            "state_snapshot": _scenario_state(folder, scenario, root),
            "human": _human(folder, root, identity),
        })
    manifest_path = Path(report["sources"][0]["path"])
    return {
        "schema_version": 1, "kind": "instructor_replay", "read_only": True,
        "root": str(root), "manifest_source": _source(manifest_path, root),
        "saved_suite_status": report["manifest"].get("status"),
        "execution_code": report["manifest"].get("code", {}),
        "report_builder": report.get("report_builder", {}),
        "export_builder": {"module": "moumou.eval.replay", "source_sha256":
                           hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        "metrics": report["metrics"], "tasks": tasks, "scenarios": scenarios,
        "protected_directories": report["protected_directories"],
        "limits": [
            "Original node/event objects are retained; absent parents/state are not invented.",
            "Scenario plus task_id identifies a task; phase labels identify saved observations.",
            "Export is a read-only handoff, not evidence of P2 consumer acceptance.",
            "External stop ledgers are not substituted for the raw suite status.",
        ],
    }


def build_replay(root: Path | str) -> dict:
    """Export a validated saved instructor suite, without changing any evidence."""
    return _from_report(build_report(root))


def generate_replay(root: Path | str, output: Path | str | None = None) -> Path:
    """Write a fresh derived JSON file; never replace raw or previous evidence."""
    model = build_replay(root)
    target = Path(output) if output is not None else Path(model["root"]) / "replay.json"
    if target.is_symlink() or target.exists():
        raise ValueError("replay output must be a fresh file")
    target = target.resolve()
    if target.suffix.lower() != ".json" or target.name in _RAW_NAMES:
        raise ValueError("replay output must be a derived .json file")
    if any(target.is_relative_to(Path(path)) for path in model["protected_directories"]):
        raise ValueError("replay output cannot enter original evidence directories")
    if target.is_relative_to(Path(model["root"]) / "reviews"):
        raise ValueError("replay output cannot enter review evidence")
    text = json.dumps(model, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation closes the exists-check race; another writer is never replaced.
    with target.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export saved instructor evidence for replay")
    parser.add_argument("--run", required=True)
    parser.add_argument("--out", help="fresh derived JSON; default RUN/replay.json")
    args = parser.parse_args(argv)
    try:
        path = generate_replay(args.run, args.out)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"cannot export saved replay: {exc}", file=sys.stderr)
        return 2
    print(f"replay exported: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
