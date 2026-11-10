"""Parent-side evaluation protocol; workers never publish authoritative results."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sys
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit
from uuid import uuid4

from moumou.adapters import DeepSeekClient, FakeLLM
from moumou.adapters.llm import LLMResponse
from moumou.eval.artifacts import atomic_json, redact, utc_now
from moumou.eval.process import supervise

MAX_CANDIDATE_BYTES = 8 * 1024 * 1024
CLEANUP_TIMEOUT_S = 5.0
_FACTORY = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*\Z")
_STATUSES = {"succeeded", "unverified", "failed", "error", "cancelled", "incomplete"}
_NODE_STATUSES = {"PENDING", "READY", "RUNNING", "WAITING_HUMAN", "DONE", "FAILED", "DEAD"}


def prepare_client(llm, worker_factory: str | None) -> tuple[dict, dict[str, str]]:
    """Return a portable public specification and separate ephemeral credentials."""
    if worker_factory is not None:
        if not isinstance(worker_factory, str) or not _FACTORY.fullmatch(worker_factory):
            raise ValueError("worker_factory must be a trusted module:function reference")
        return {"kind": "factory", "reference": worker_factory}, {}
    if isinstance(llm, DeepSeekClient):
        if not llm.api_key:
            raise ValueError("the worker client requires an API credential")
        url = urlsplit(llm.base_url)
        if (url.scheme not in ("https", "http") or not url.hostname
                or url.username or url.password or url.query or url.fragment):
            raise ValueError("worker base_url must not contain URL credentials or query data")
        if any(llm.api_key in str(value) for value in (llm.base_url, llm.model)):
            raise ValueError("a credential cannot be part of public worker configuration")
        return {
            "kind": "deepseek", "base_url": llm.base_url, "model": llm.model,
            "timeout_s": llm.timeout_s, "max_retries": llm.max_retries,
        }, {"MOUMOU_EVAL_API_KEY": llm.api_key}
    if isinstance(llm, FakeLLM):
        script = llm._script
        if not isinstance(script, list) or not all(isinstance(x, LLMResponse) for x in script):
            raise ValueError("callable FakeLLM requires an explicit worker_factory in process mode")
        return {"kind": "fake", "responses": [x.model_dump(mode="json") for x in script]}, {}
    raise ValueError("process mode needs DeepSeekClient, queued FakeLLM, or worker_factory")


def validate_timeout(value) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError("hard_timeout_s must be a finite positive number")
    if sys.platform != "win32":
        raise NotImplementedError("Hard evaluation workers currently require native Windows")
    return float(value)


def _read_json(path: Path, limit: int) -> dict:
    if path.is_symlink() or path.resolve().parent != path.parent.resolve():
        raise ValueError("worker response must be a regular file in its protocol directory")
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError("worker response is missing or exceeds the size limit")
    def invalid_constant(value):
        raise ValueError("worker JSON contains a non-finite number")

    data = json.loads(path.read_text(encoding="utf-8"), parse_constant=invalid_constant)
    if not isinstance(data, dict):
        raise ValueError("worker response must be a JSON object")
    return data


def _integer(value, *, nullable=True, nonnegative=True):
    return (value is None and nullable) or (
        isinstance(value, int) and not isinstance(value, bool) and (value >= 0 or not nonnegative)
    )


def _number(value, *, nullable=True):
    return (value is None and nullable) or (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value) and value >= 0
    )


def validate_candidate(path, *, nonce, fixture, repeat, eval_id, run_dir):
    """Strict protocol checks, including numeric types which dataclasses do not validate."""
    from moumou.eval.runner import FixtureResult

    envelope = _read_json(path, MAX_CANDIDATE_BYTES)
    if (type(envelope.get("schema_version")) is not int or envelope["schema_version"] != 1
            or envelope.get("nonce") != nonce
            or set(envelope) != {"schema_version", "nonce", "result"}):
        raise ValueError("worker envelope version or nonce does not match this attempt")
    data = envelope["result"]
    if not isinstance(data, dict) or set(data) != {item.name for item in fields(FixtureResult)}:
        raise ValueError("worker result fields do not match the protocol schema")
    if (type(data["schema_version"]) is not int or data["schema_version"] != 1
            or type(data["attempt"]) is not int or data["attempt"] != 1
            or data["fixture_id"] != fixture.id or data["scenario"] != fixture.scenario
            or type(data["repeat"]) is not int or data["repeat"] != repeat
            or data["eval_id"] != eval_id or not isinstance(data["run_dir"], str)
            or Path(data["run_dir"]).resolve() != run_dir.resolve()):
        raise ValueError("worker result identity does not match this attempt")
    if not isinstance(data["status"], str) or data["status"] not in _STATUSES:
        raise ValueError("invalid worker result status")
    integers = (
        "done", "dead", "total", "tokens_in", "tokens_out", "unfinished", "usage_missing_calls",
        "total_tokens_in", "total_tokens_out", "worker_pid",
    )
    if any(not _integer(data[key]) for key in integers) or not _integer(
        data["worker_exit_code"], nonnegative=False
    ):
        raise ValueError("worker counts must be nullable non-negative integers")
    for key in ("wall_s", "elapsed_s", "completion_rate", "cleanup_s", "hard_timeout_s"):
        if not _number(data[key]):
            raise ValueError("worker measurements must be nullable finite non-negative numbers")
    if data["completion_rate"] is not None and data["completion_rate"] > 1:
        raise ValueError("worker completion rate must be between zero and one")
    for key in ("root_machine_accepted", "root_success", "cleanup_complete"):
        if data[key] is not None and type(data[key]) is not bool:
            raise ValueError("worker verdicts must be nullable booleans")
    for key in (
        "error_type", "error_message", "root_task_id", "root_status", "started_at", "finished_at",
    ):
        if data[key] is not None and not isinstance(data[key], str):
            raise ValueError("worker textual observations have invalid types")
    for key in ("started_at", "finished_at"):
        if data[key] is not None and datetime.fromisoformat(data[key]).tzinfo is None:
            raise ValueError("worker timestamps must identify their time zone")
    if data["root_status"] is not None and data["root_status"] not in _NODE_STATUSES:
        raise ValueError("invalid worker root status")
    if data["execution_mode"] != "in_process":
        raise ValueError("a worker candidate cannot claim parent supervision metadata")
    if not isinstance(data["errors"], list) or not all(isinstance(x, str) for x in data["errors"]):
        raise ValueError("worker errors must be a list of text")
    if not isinstance(data["nodes"], list):
        raise ValueError("worker nodes must be a list")
    node_ids = set()
    for node in data["nodes"]:
        if (not isinstance(node, dict) or not isinstance(node.get("id"), str)
                or not isinstance(node.get("role"), str)
                or node.get("status") not in _NODE_STATUSES
                or not _integer(node.get("retries"), nullable=False)
                or not _integer(node.get("acceptance_count"), nullable=False)
                or not isinstance(node.get("artifacts"), list)
                or not all(isinstance(x, str) for x in node["artifacts"])):
            raise ValueError("invalid worker node observation")
        if (not node["id"] or node["id"] in node_ids
                or (node.get("parent_id") is not None and not isinstance(node["parent_id"], str))
                or (node.get("error") is not None and not isinstance(node["error"], str))):
            raise ValueError("invalid or duplicate worker node identity")
        node_ids.add(node["id"])
    nodes = {node["id"]: node for node in data["nodes"]}
    root = nodes.get(data["root_task_id"])
    if nodes:
        roots = [node for node in nodes.values() if node.get("parent_id") is None]
        if len(roots) != 1 or root is not roots[0] or root["status"] != data["root_status"]:
            raise ValueError("worker root identity/status does not match its nodes")
        for node in nodes.values():
            seen = set()
            while node.get("parent_id") is not None:
                if node["id"] in seen or node["parent_id"] not in nodes:
                    raise ValueError("worker dependency tree is invalid")
                seen.add(node["id"])
                node = nodes[node["parent_id"]]
        expected_counts = {
            "done": sum(n["status"] == "DONE" for n in nodes.values()),
            "dead": sum(n["status"] == "DEAD" for n in nodes.values()),
            "unfinished": sum(n["status"] not in {"DONE", "DEAD"} for n in nodes.values()),
        }
        if any(
            data[key] is not None and data[key] != count for key, count in expected_counts.items()
        ):
            raise ValueError("worker counts disagree with the node states")
    if data["total"] is not None:
        if data["total"] != len(data["nodes"]):
            raise ValueError("worker node total does not match the observations")
        counts = [data[k] for k in ("done", "dead", "unfinished")]
        if all(value is not None for value in counts) and sum(counts) != data["total"]:
            raise ValueError("worker node counts are inconsistent")
    checks = data["root_acceptance_checks"]
    if not isinstance(checks, list) or any(
        not isinstance(check, dict) or not isinstance(check.get("kind"), str)
        or type(check.get("ok")) is not bool for check in checks
    ):
        raise ValueError("invalid worker acceptance observations")
    if fixture.acceptance and checks:
        if len(checks) != len(fixture.acceptance) or any(
            observed.get("kind") != expected.kind or observed.get("target") != expected.target
            for observed, expected in zip(checks, fixture.acceptance, strict=True)
        ):
            raise ValueError("worker acceptance checks do not match the fixture")
        expected_machine = all(check["ok"] for check in checks)
        if data["root_machine_accepted"] is not expected_machine:
            raise ValueError("worker machine verdict disagrees with its checks")
    elif data["root_machine_accepted"] is not None:
        raise ValueError("worker machine verdict has no matching machine checks")
    if root is not None:
        expected_success = data["root_machine_accepted"] if root["status"] == "DONE" else False
        if data["root_success"] is not expected_success:
            raise ValueError("worker root success disagrees with its observations")
    if data["status"] == "succeeded" and not (
        data["root_status"] == "DONE" and data["root_success"] is True
        and data["root_machine_accepted"] is True and fixture.acceptance
        and len(checks) == len(fixture.acceptance)
        and all(c["ok"] and c["kind"] != "self_report" for c in checks)
    ):
        raise ValueError("a successful worker needs actual root machine acceptance")
    paths = data["artifacts"]
    if not isinstance(paths, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        or not Path(value).resolve().is_relative_to(run_dir.resolve())
        for key, value in paths.items()
    ):
        raise ValueError("worker artifact paths must remain inside the run")
    return FixtureResult(**data)


async def run_isolated(
    fixture, llm, root: Path, repeat: int, *, workspace_backend: str, eval_id: str,
    hard_timeout_s: float, client_spec: dict, client_env: dict[str, str],
):
    from moumou.eval.runner import _empty_result

    root = root.resolve()
    run_dir = root / fixture.id / f"r{repeat}"
    if run_dir.exists():
        raise FileExistsError("an isolated attempt cannot reuse a run directory")
    directory = root / ".workers" / fixture.id / f"r{repeat}"
    directory.mkdir(parents=True, exist_ok=False)
    nonce = uuid4().hex
    started, started_at = monotonic(), utc_now()
    request_path = directory / "request.json"
    candidate_path = directory / "candidate.json"
    atomic_json(request_path, {
        "schema_version": 1, "nonce": nonce, "fixture": fixture.model_dump(mode="json"),
        "root": str(root), "repeat": repeat, "eval_id": eval_id,
        "workspace_backend": workspace_backend, "client": client_spec,
    })
    child_env = dict(os.environ)
    child_env.pop("DEEPSEEK_API_KEY", None)
    child_env.pop("MOUMOU_EVAL_API_KEY", None)
    child_env.update(client_env)
    # Carry normal import roots, including trusted test factories, without
    # pickling clients, HTTP pools or callback closures across processes.
    child_env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(
        str(Path(value or os.getcwd()).resolve()) for value in sys.path
    ))
    child_env["PYTHONIOENCODING"] = "utf-8"

    def started_worker(pid):
        atomic_json(directory / "supervisor.json", {
            "schema_version": 1, "nonce": nonce, "pid": pid,
            "phase": "contained_before_resume", "hard_timeout_s": hard_timeout_s,
        })

    outcome = await supervise(
        [sys.executable, "-m", "moumou.eval.worker", "--request", str(request_path)],
        cwd=Path(__file__).resolve().parents[3], env=child_env,
        timeout_s=hard_timeout_s, cleanup_timeout_s=CLEANUP_TIMEOUT_S,
        on_started=started_worker,
    )
    summary = outcome.asdict()
    summary["error"] = redact(summary["error"], llm) if summary["error"] else None
    atomic_json(directory / "supervisor.json", {"schema_version": 1, "nonce": nonce, **summary})
    result = _empty_result(fixture, run_dir, repeat, eval_id)
    error_type = {
        "timeout": "timeout", "start_failed": "worker_start_failed",
        "cleanup_failed": "cleanup_failed", "cancelled": "cancelled",
    }.get(outcome.status)
    if error_type is None and outcome.exit_code != 0:
        error_type = "worker_crash"
        try:
            diagnostic = _read_json(directory / "worker-error.json", 64 * 1024)
            if diagnostic.get("stage") in {"request", "client"}:
                error_type = "worker_start_failed"
        except Exception:
            diagnostic = {}
        message = diagnostic.get("message")
        result.error_message = redact(message, llm) if isinstance(message, str) else None
    if error_type is None:
        try:
            result = validate_candidate(
                candidate_path, nonce=nonce, fixture=fixture, repeat=repeat,
                eval_id=eval_id, run_dir=run_dir,
            )
        except Exception as exc:
            # This boundary consumes an untrusted protocol file, including
            # malformed nesting/paths; it must not discard supervision evidence.
            error_type = "invalid_worker_result"
            result.error_message = redact(str(exc), llm)[:2000]
    if error_type is not None:
        result.status = "cancelled" if outcome.cancelled else "error"
        result.error_type = error_type
        result.root_success = False
        result.error_message = result.error_message or summary["error"] or (
            f"Worker {error_type}; exit code {outcome.exit_code}"
        )
        result.errors.append(result.error_message)
    result.execution_mode = "process"
    result.hard_timeout_s = hard_timeout_s
    result.worker_pid, result.worker_exit_code = outcome.pid, outcome.exit_code
    result.cleanup_s, result.cleanup_complete = outcome.cleanup_s, outcome.cleanup_complete
    result.started_at, result.finished_at = started_at, utc_now()
    result.elapsed_s = round(monotonic() - started, 6)
    result.artifacts.update({
        "worker_request": str(request_path), "worker_candidate": str(candidate_path),
        "worker_supervisor": str(directory / "supervisor.json"),
        "worker_diagnostic": str(directory / "worker-error.json"),
        "worker_client": str(directory / "worker-client.json"),
    })
    # Candidate, raw result and trace remain untouched. Only the parent publishes
    # the authoritative record, after containment has been verified or flagged.
    run_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(run_dir / "result.json", asdict(result))
    if outcome.cancelled:
        raise asyncio.CancelledError
    return result
