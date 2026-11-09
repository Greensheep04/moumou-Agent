"""Evaluation provenance and durable result files.

The batch runner is the sole writer. A per-run ``result.json`` is authoritative;
``results.jsonl`` is a convenient index and may end with a partial line after a
process crash. Reading completed results does not resume or reuse a batch.

Atomic replacement applies to one file, not the whole batch. Hard process
termination and worker isolation belong to the timeout layer. Engine-reported
token counts retain the engine's current accounting limits.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit
from uuid import uuid4

from moumou.adapters import FakeLLM
from moumou.eval.loader import TaskFixture
from moumou.orchestrator import Settings

SCHEMA_VERSION = 1
_EVAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False)


def atomic_text(path: Path, text: str) -> None:
    """Flush a same-directory temporary file before atomically replacing one file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_json(path: Path, data: dict) -> None:
    atomic_text(path, _json(data) + "\n")


def redact(text: str, llm) -> str:
    """Remove a client's known key and common credential forms from diagnostics."""
    key = getattr(llm, "api_key", None)
    if isinstance(key, str) and key:
        text = text.replace(key, "[REDACTED]")
    text = re.sub(r"(?i)(Bearer\s+)[^\s\"'<>]+", r"\1[REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", text)
    return text


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _code_version() -> dict:
    repository = Path(__file__).resolve().parents[3]
    files = sorted((repository / "src").rglob("*.py"))
    project = repository / "pyproject.toml"
    if project.is_file():
        files.append(project)
    hashes = {
        path.relative_to(repository).as_posix(): _sha(path.read_bytes())
        for path in sorted(files)
    }
    version = {
        "git_commit": None,
        "git_dirty": None,
        "source_sha256": _sha(_json(hashes).encode("utf-8")),
        "source_files": hashes,
        "source_scope": "src/**/*.py and pyproject.toml; secrets and run artifacts excluded",
    }
    git = ["git", "-c", f"safe.directory={repository.as_posix()}"]
    try:
        commit = subprocess.run(
            [*git, "rev-parse", "HEAD"], cwd=repository, check=True,
            capture_output=True, text=True, encoding="utf-8", timeout=5,
        )
        status = subprocess.run(
            [*git, "status", "--porcelain", "--untracked-files=normal"],
            cwd=repository, check=True, capture_output=True, text=True,
            encoding="utf-8", timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    else:
        version["git_commit"] = commit.stdout.strip()
        version["git_dirty"] = bool(status.stdout.strip())
    return version


def _model_metadata(llm) -> dict:
    """Only public, explicitly chosen fields; never serialize the client or env."""
    if isinstance(llm, FakeLLM):
        return {"provider": "fake", "model": "FakeLLM", "client": type(llm).__name__}
    metadata = {
        "provider": None,
        "model": getattr(llm, "model", None),
        "client": type(llm).__name__,
        "endpoint_host": None,
    }
    if not isinstance(metadata["model"], str):
        metadata["model"] = None
    base_url = getattr(llm, "base_url", None)
    if isinstance(base_url, str):
        try:
            host = urlsplit(base_url).hostname
        except ValueError:
            host = None
        metadata["endpoint_host"] = host
        if host:
            if "hkgai" in host.lower():
                metadata["provider"] = "hkgai"
            elif "deepseek" in host.lower():
                metadata["provider"] = "deepseek"
            else:
                metadata["provider"] = host
    for field in ("timeout_s", "max_retries"):
        value = getattr(llm, field, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            metadata[field] = value
    # Model names are public configuration but must not accidentally repeat a key.
    return {k: redact(v, llm) if isinstance(v, str) else v for k, v in metadata.items()}


def create_manifest(
    root: Path,
    fixtures: list[TaskFixture],
    llm,
    repeats: int | None,
    workspace_backend: str,
    *,
    eval_id: str | None = None,
) -> dict:
    """Write a new batch's plan and current source/configuration provenance.

    The caller must reserve a fresh root and preflight fixture identities first.
    There is deliberately no resume mode here.
    """
    root = Path(root).resolve()
    if eval_id is None:
        eval_id = f"eval-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex}"
    if (not isinstance(eval_id, str) or not _EVAL_ID.fullmatch(eval_id)
            or eval_id.endswith(".") or PureWindowsPath(eval_id).is_reserved()):
        raise ValueError("eval_id must be one safe identifier")
    dataset = [
        {
            "fixture_id": fixture.id,
            "scenario": fixture.scenario,
            "sha256": _sha(_json(fixture.model_dump(mode="json")).encode("utf-8")),
            "repeats": repeats if repeats is not None else fixture.repeats,
        }
        for fixture in fixtures
    ]
    runs = [
        {"fixture_id": item["fixture_id"], "repeat": repeat,
         "run_dir": f"{item['fixture_id']}/r{repeat}"}
        for item in dataset
        for repeat in range(item["repeats"])
    ]
    defaults = Settings().model_dump(
        mode="json", exclude={"project_id", "conversation_id", "root"}
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "eval_id": eval_id,
        "status": "running",
        "started_at": utc_now(),
        "finished_at": None,
        "purpose": "offline_validation" if isinstance(llm, FakeLLM) else "model_evaluation",
        "code": _code_version(),
        "dataset": {
            "sha256": _sha(_json({"fixtures": dataset}).encode("utf-8")),
            "fixtures": dataset,
            "hash_format": "canonical model_dump(mode=json), sorted keys, UTF-8",
        },
        "model": _model_metadata(llm),
        "settings": {
            "engine": defaults,
            "workspace_backend": workspace_backend,
            "harness_concurrency": 1,
            "repeats_override": repeats,
            "project_id": "fixture_id",
            "conversation_id": "c1",
            "fresh_state_per_run": True,
            "memory_warmup": False,
        },
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "interpreter": sys.executable,
        },
        "runs": runs,
        "planned_runs": len(runs),
        "completed_runs": 0,
        "measurement_limits": [
            "Engine wall time excludes its post-run maintenance and role review.",
            "Engine token counts may omit later LLM calls; full usage coverage is uncomputed.",
            "Per-run result.json is authoritative; JSONL and manifest are not one transaction.",
            "Hard process kill recovery and worker isolation are not supplied by this module.",
        ],
    }
    with (root / "results.jsonl").open("x", encoding="utf-8") as stream:
        stream.flush()
        os.fsync(stream.fileno())
    atomic_json(root / "manifest.json", manifest)
    return manifest


def append_result(root: Path, data: dict) -> None:
    """Append one index row after the authoritative per-run file has been saved."""
    line = _json(data) + "\n"
    with (Path(root) / "results.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())


def load_results(root: Path) -> list[dict]:
    """Read completed per-run files, even if the JSONL index is absent or truncated.

    Missing results represent planned but unpersisted runs. Invalid identity or
    escaping paths raise an error instead of silently mixing batches.
    """
    root = Path(root).resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION
            or not isinstance(manifest.get("eval_id"), str)
            or not isinstance(manifest.get("runs"), list)):
        raise ValueError("unsupported or invalid evaluation manifest")
    results, identities, directories = [], set(), set()
    for run in manifest["runs"]:
        if not isinstance(run, dict):
            raise ValueError("invalid planned run")
        fixture_id, repeat, relative = (run.get(k) for k in ("fixture_id", "repeat", "run_dir"))
        if (not isinstance(fixture_id, str) or not isinstance(repeat, int)
                or isinstance(repeat, bool) or repeat < 0 or not isinstance(relative, str)):
            raise ValueError("invalid planned run identity")
        windows = PureWindowsPath(relative)
        if (windows.drive or windows.root or ".." in windows.parts or ":" in relative
                or not relative or any(part.endswith((".", " ")) for part in windows.parts)):
            raise PermissionError("planned run path escapes or aliases the batch")
        directory = (root / relative.replace("\\", "/")).resolve()
        if not directory.is_relative_to(root) or directory == root:
            raise PermissionError("planned run path escapes the batch")
        identity = (fixture_id, repeat)
        directory_key = str(directory).casefold()
        if identity in identities or directory_key in directories:
            raise ValueError("duplicate planned run")
        identities.add(identity)
        directories.add(directory_key)
        path = (directory / "result.json").resolve()
        if not path.is_relative_to(directory):
            raise PermissionError("result path escapes its run directory")
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("eval_id") != manifest["eval_id"]
                or data.get("fixture_id") != fixture_id or data.get("repeat") != repeat
                or not isinstance(data.get("run_dir"), str)
                or Path(data["run_dir"]).resolve() != directory):
            raise ValueError("result identity does not match its planned run")
        results.append(data)
    return results
