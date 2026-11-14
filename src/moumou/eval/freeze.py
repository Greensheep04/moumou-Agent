"""Call-free experiment snapshots and fail-closed readiness checks.

A snapshot is evidence, not an executor. This module never loads .env, creates a
model client, changes Git, or claims that an archived runtime was already run.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path, PureWindowsPath
from uuid import uuid4

from moumou.contracts import Budget
from moumou.eval.ablation import PROTOCOL_VERSION, _check_path_budget, plan_ablation
from moumou.eval.artifacts import atomic_json, atomic_text, utc_now
from moumou.eval.loader import TaskFixture, load_tasks
from moumou.eval.workspace import BACKENDS, check_batch_dir

SCHEMA_VERSION = 1
MAX_JSON_BYTES = 32 * 1024 * 1024
_DEPENDENCIES = ("pydantic", "httpx", "httpcore", "h11", "certifi", "PyYAML",
                 "pytest", "pytest-asyncio", "ruff", "hatchling")
_SECRET = re.compile(r"\b(?:sk-|tvly-)[A-Za-z0-9_-]{8,}|\bBearer\s+\S+", re.IGNORECASE)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_json(path: Path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError("missing, linked or oversized experiment JSON")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate experiment JSON field")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError("non-finite experiment JSON number")

    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=pairs,
                      parse_constant=invalid)


def _relative(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("snapshot path must be text")
    win = PureWindowsPath(value)
    if (not value or win.drive or win.root or "\\" in value or ":" in value
            or any(part in {"", ".", ".."} or part.endswith((".", " "))
                   or PureWindowsPath(part).is_reserved() for part in value.split("/"))):
        raise ValueError("snapshot path escapes or aliases its root")
    return value


def _inventory(repo: Path) -> dict[str, bytes]:
    repo = repo.resolve()
    source = repo / "src"
    paths = sorted(source.rglob("*.py"))
    if not paths or not (repo / "pyproject.toml").is_file():
        raise ValueError("freeze needs runtime Python files and pyproject.toml")
    paths += [repo / "pyproject.toml", *sorted(repo.glob("requirements*.lock"))]
    result = {}
    for path in paths:
        if (not path.resolve().is_relative_to(repo) or path.is_symlink()
                or any(parent.is_symlink() for parent in path.parents if parent != repo
                       and parent.is_relative_to(repo))):
            raise ValueError("runtime snapshot cannot follow source symlinks")
        relative = path.relative_to(repo).as_posix()
        _relative(relative)
        result[relative] = path.read_bytes()
    return dict(sorted(result.items()))


def _environment() -> dict:
    packages = {}
    for name in _DEPENDENCIES:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {"python": platform.python_version(), "implementation": platform.python_implementation(),
            "platform": sys.platform, "system": platform.system(), "packages": packages}


def _provenance(repo: Path, inventory: dict[str, bytes]) -> dict:
    result = {"git_commit": None, "git_dirty": None, "working_tree_status": None}
    command = ["git", "-c", f"safe.directory={repo.as_posix()}"]
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        commit = subprocess.run([*command, "rev-parse", "HEAD"], cwd=repo, env=env,
                                capture_output=True, check=True, timeout=5)
        status = subprocess.run([*command, "status", "--porcelain", "--untracked-files=normal"],
                                cwd=repo, env=env, capture_output=True, check=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass
    else:
        result.update(git_commit=commit.stdout.decode("utf-8").strip(),
                      git_dirty=bool(status.stdout.strip()),
                      working_tree_status=status.stdout.decode("utf-8").splitlines())
    hashes = {path: _sha(data) for path, data in inventory.items()}
    result.update(source_files=hashes, source_sha256=_sha(_json_bytes(hashes)),
                  source_scope="src/**/*.py, pyproject.toml and requirements*.lock; exact bytes",
                  dirty_policy="Dirty snapshots are explicit; no commit is created or invented.")
    return result


def _model_config(config: Mapping) -> dict:
    from urllib.parse import urlsplit

    if not isinstance(config, Mapping) or set(config) - {
        "provider", "model", "base_url", "timeout_s", "max_retries", "temperature",
    }:
        raise ValueError("model_config contains unsupported or secret fields")
    model, base_url = config.get("model"), config.get("base_url")
    if (not isinstance(model, str) or not model.strip() or len(model) > 200
            or not isinstance(base_url, str) or _SECRET.search(model + base_url)):
        raise ValueError("public model configuration must not contain credentials")
    url = urlsplit(base_url)
    if (url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password
            or url.query or url.fragment):
        raise ValueError("model endpoint must not contain credentials, query or fragment")
    host = url.hostname.lower()
    provider = config.get("provider") or (
        "hkgai" if "hkgai" in host else "deepseek" if "deepseek" in host else None
    )
    if provider not in {"hkgai", "deepseek"} or provider not in host:
        raise ValueError("freeze requires a matching DeepSeek or HKGAI provider")
    timeout, retries = config.get("timeout_s", 180.0), config.get("max_retries", 3)
    temperature = config.get("temperature", 0.2)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("HTTP timeout must be finite and positive")
    if type(retries) is not int or not 1 <= retries <= 10:
        raise ValueError("HTTP retries must be an integer between one and ten")
    if isinstance(temperature, bool) or temperature != 0.2:
        raise ValueError("the current client fixes temperature at 0.2")
    return {"provider": provider, "model": model, "base_url": base_url.rstrip("/"),
            "timeout_s": float(timeout), "max_retries": retries, "temperature": 0.2}


def _capabilities(values: Mapping) -> dict:
    if (not isinstance(values, Mapping) or set(values) - {"web_search"}
            or any(type(value) is not bool for value in values.values())):
        raise ValueError("capabilities accepts only boolean web_search; never pass keys or env")
    return {"web_search": values.get("web_search", False)}


def _budget_override(value) -> dict:
    if value is None:
        return {}
    if isinstance(value, Budget):
        value = value.model_dump(exclude_unset=True)
    if (not isinstance(value, Mapping) or set(value) - set(Budget.model_fields)
            or any(type(item) is not int for item in value.values())):
        raise ValueError("budget override must contain only integer Budget fields")
    Budget.model_validate(dict(value))
    return dict(value)


def _effective(fixtures, heldout, warmup):
    result = []
    for fixture in fixtures:
        data = fixture.model_dump(mode="json")
        data["budget"].update(heldout)
        if data.get("warmup") is not None:
            data["warmup"]["budget"].update(warmup)
        result.append(TaskFixture.model_validate(data))
    return result


def _plan(fixtures, repeats, capabilities):
    plan = plan_ablation(fixtures, repeats)
    by_id = {fixture.id: fixture for fixture in fixtures}
    blocked = {}
    for row in plan["schedule"]:
        fixture = by_id[row["fixture_id"]]
        needs = ["web_search"] if "web_search" in fixture.requirements.required_tools else []
        missing = [name for name in needs if not capabilities[name]]
        row.update(eligibility="blocked_not_started" if missing else "ready",
                   status="blocked_not_started" if missing else "not_started",
                   required_capabilities=needs, missing_capabilities=missing)
        if missing:
            blocked[fixture.id] = missing
    count = sum(row["eligibility"] == "ready" for row in plan["schedule"])
    readiness = {"planned_runs": plan["planned_runs"], "ready_count": count,
                 "blocked_count": plan["planned_runs"] - count, "started_runs": 0,
                 "completed_runs": 0, "full_run_ready": count == plan["planned_runs"],
                 "blocked_fixtures": [{"fixture_id": name, "missing_capabilities": reasons}
                                      for name, reasons in sorted(blocked.items())],
                 "capability_check": "caller-declared configuration; no service request performed",
                 "scope": "external capabilities; check-start also verifies runtime and platform"}
    return plan, readiness


def prepare_experiment(
    fixtures, out: Path, *, repeats: int = 3, model_config: Mapping, capabilities: Mapping,
    workspace_backend: str = "local", hard_timeout_s: float = 180,
    budget_override=None, warmup_budget_override=None,
) -> dict:
    """Persist a fresh immutable-by-hash plan and allowlisted runtime copies.

    Returns exactly the persisted freeze manifest. The separate freeze.sha256
    checks its bytes; these ordinary hashes are not an authenticated signature.
    Budget overrides are per node, not an experiment-wide or monetary ceiling.
    """
    if workspace_backend not in BACKENDS:
        raise ValueError("unsupported workspace backend")
    if (isinstance(hard_timeout_s, bool) or not isinstance(hard_timeout_s, (int, float))
            or not math.isfinite(hard_timeout_s) or hard_timeout_s <= 0):
        raise ValueError("hard_timeout_s must be finite and positive")
    model, caps = _model_config(model_config), _capabilities(capabilities)
    original = [TaskFixture.model_validate(fixture).model_dump(mode="json") for fixture in fixtures]
    heldout, warmup = _budget_override(budget_override), _budget_override(warmup_budget_override)
    effective = _effective([TaskFixture.model_validate(data) for data in original], heldout, warmup)
    plan, readiness = _plan(effective, repeats, caps)
    repo, out = _repository_root().resolve(), Path(out)
    if out.is_symlink():
        raise ValueError("freeze output cannot be a symlink")
    out = out.resolve()
    if out == repo or out.is_relative_to(repo / "src") or out.is_relative_to(repo / ".git"):
        raise ValueError("freeze output cannot replace source or Git metadata")
    check_batch_dir(out)
    inventory = _inventory(repo)
    path_preflight = _check_path_budget(out / "execution", effective, plan, workspace_backend)
    for relative in inventory:
        path = out / "snapshot" / relative
        if sys.platform == "win32" and len(str(path).encode("utf-16-le")) // 2 > 259:
            raise ValueError("freeze snapshot path is too long; use a shorter --out directory")
    code = _provenance(repo, inventory)
    canonical_fixtures = [fixture.model_dump(mode="json") for fixture in effective]
    all_files = {**inventory, "fixtures.json": _json_bytes(canonical_fixtures)}
    manifest = {
        "schema_version": SCHEMA_VERSION, "kind": "frozen_experiment",
        "experiment_id": "freeze-" + uuid4().hex[:16], "created_at": utc_now(),
        "status": "prepared_not_started", "dataset_source": "self_built",
        "code": code, "environment": _environment(), "fixtures": canonical_fixtures,
        "original_fixtures": original, "dataset_sha256": _sha(all_files["fixtures.json"]),
        "model_config": model, "capabilities": caps,
        "budget_overrides": {"heldout": heldout, "warmup": warmup,
                             "scope": "per-task nodes; not total experiment cost"},
        "settings": {"workspace_backend": workspace_backend,
                     "hard_timeout_s": float(hard_timeout_s), "role_review_enabled": False,
                     "memory_scope": "long-term experience only; conclusion board enabled"},
        "protocol_version": PROTOCOL_VERSION, "plan": plan, "readiness": readiness,
        "snapshot": {"directory": "snapshot", "files": {
            path: {"sha256": _sha(data), "bytes": len(data)} for path, data in all_files.items()
        }},
        "path_preflight": path_preflight,
        "execution": {"started": False, "model_calls": 0,
                      "frozen_runtime_automatically_executed": False,
                      "runtime_policy": (
                          "verify current runtime before use; snapshot is replay evidence"
                      ),
                      "resume_implemented": False},
        "limits": ["No client, .env reader, service check or Git write is invoked.",
                   "Full readiness requires every declared external capability.",
                   "A non-network guard keeps blocked jobs in the complete plan.",
                   "Snapshot hashes detect corruption, not an adversary replacing every hash.",
                   "Warmup has a cooperative in-process budget, not held-out hard containment.",
                   "Known Windows paths are checked; future generated paths are not guaranteed."],
    }
    out.mkdir(parents=True, exist_ok=True)
    with (out / ".freeze").open("x", encoding="utf-8") as marker:
        marker.write("Experiment snapshot reserved; use a fresh directory.\n")
    for relative, data in all_files.items():
        target = out / "snapshot" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    if _inventory(repo) != inventory:
        raise ValueError("runtime changed during freeze preparation; use a new snapshot")
    atomic_json(out / "freeze.json", manifest)
    atomic_text(out / "freeze.sha256", _sha((out / "freeze.json").read_bytes()) + "\n")
    return manifest


def verify_experiment(freeze_dir: Path, *, check_current: bool = True) -> dict:
    """Verify archive consistency and, by default, the current runtime/environment."""
    root = Path(freeze_dir)
    if root.is_symlink():
        raise ValueError("freeze directory cannot be a symlink")
    root = root.resolve()
    path = root / "freeze.json"
    digest_path = root / "freeze.sha256"
    if (digest_path.is_symlink() or not digest_path.is_file()
            or digest_path.stat().st_size > 128):
        raise ValueError("freeze manifest fingerprint is missing")
    data = _read_json(path)
    digest = digest_path.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or _sha(path.read_bytes()) != digest:
        raise ValueError("freeze manifest fingerprint mismatch")
    if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
            or data["schema_version"] != SCHEMA_VERSION or data.get("kind") != "frozen_experiment"
            or data.get("protocol_version") != PROTOCOL_VERSION
            or data.get("status") != "prepared_not_started"):
        raise ValueError("unsupported or invalid experiment freeze")
    for name in ("code", "environment", "model_config", "capabilities", "budget_overrides",
                 "settings", "plan", "readiness", "snapshot", "execution"):
        if not isinstance(data.get(name), dict):
            raise ValueError("invalid freeze metadata shape")
    if (not isinstance(data.get("fixtures"), list) or not data["fixtures"]
            or not isinstance(data.get("original_fixtures"), list)):
        raise ValueError("invalid frozen fixture collection")
    model, caps = _model_config(data["model_config"]), _capabilities(data["capabilities"])
    if model != data["model_config"] or caps != data["capabilities"]:
        raise ValueError("freeze model or capabilities are not canonical")
    fixtures = [TaskFixture.model_validate(item) for item in data["fixtures"]]
    heldout = _budget_override(data["budget_overrides"]["heldout"])
    warmup = _budget_override(data["budget_overrides"]["warmup"])
    reconstructed = _effective([TaskFixture.model_validate(item)
                               for item in data["original_fixtures"]], heldout, warmup)
    if [item.model_dump(mode="json") for item in reconstructed] != data["fixtures"]:
        raise ValueError("effective fixtures disagree with the original specs and overrides")
    settings = data["settings"]
    timeout = settings.get("hard_timeout_s")
    if (settings.get("workspace_backend") not in BACKENDS
            or type(settings.get("role_review_enabled")) is not bool
            or settings["role_review_enabled"] is not False
            or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("invalid frozen execution settings")
    if (data["execution"].get("started") is not False
            or type(data["execution"].get("model_calls")) is not int
            or data["execution"]["model_calls"] != 0):
        raise ValueError("immutable freeze cannot claim execution progress")
    expected_plan, readiness = _plan(fixtures, data["plan"]["repeats"], caps)
    if expected_plan != data["plan"] or readiness != data["readiness"]:
        raise ValueError("freeze schedule/readiness disagrees with fixtures and capabilities")
    files = data["snapshot"]["files"]
    if not isinstance(files, dict) or data["snapshot"].get("directory") != "snapshot":
        raise ValueError("invalid snapshot inventory")
    snapshot = root / "snapshot"
    if snapshot.is_symlink():
        raise ValueError("snapshot directory cannot be a symlink")
    for relative, expected in files.items():
        _relative(relative)
        if not (relative == "fixtures.json" or relative == "pyproject.toml"
                or relative.startswith("src/") and relative.endswith(".py")
                or "/" not in relative and re.fullmatch(r"requirements[^/]*\.lock", relative)):
            raise ValueError("snapshot contains a non-allowlisted file")
        target = snapshot / relative
        if (not isinstance(expected, dict) or target.is_symlink()
                or not target.resolve().is_relative_to(snapshot.resolve())
                or any(parent.is_symlink() for parent in target.parents
                       if parent != snapshot and parent.is_relative_to(snapshot))
                or not target.is_file() or type(expected.get("bytes")) is not int
                or target.stat().st_size != expected["bytes"]
                or _sha(target.read_bytes()) != expected.get("sha256")):
            raise ValueError("snapshot file missing, escaped or changed")
    actual_paths = {path.relative_to(snapshot).as_posix() for path in snapshot.rglob("*")
                    if path.is_file()}
    if actual_paths != set(files):
        raise ValueError("snapshot contains missing or unrecorded files")
    fixture_bytes = _json_bytes(data["fixtures"])
    if ((snapshot / "fixtures.json").read_bytes() != fixture_bytes
            or _sha(fixture_bytes) != data["dataset_sha256"]):
        raise ValueError("frozen fixture content/hash mismatch")
    runtime_files = {name: record["sha256"] for name, record in files.items()
                     if name != "fixtures.json"}
    if (runtime_files != data["code"]["source_files"]
            or _sha(_json_bytes(runtime_files)) != data["code"]["source_sha256"]):
        raise ValueError("freeze source hash disagrees with snapshot")
    if check_current:
        try:
            current = {name: _sha(value) for name, value in _inventory(_repository_root()).items()}
        except (OSError, ValueError) as exc:
            raise ValueError(
                "current runtime cannot be verified against the frozen snapshot"
            ) from exc
        if current != runtime_files:
            raise ValueError("current runtime differs from the frozen snapshot")
        if _environment() != data["environment"]:
            raise ValueError("current environment differs from the frozen snapshot")
    return {"valid": True, "experiment_id": data["experiment_id"], "freeze_sha256": digest,
            "snapshot_files": len(files), "current_runtime_checked": check_current,
            "planned_runs": data["plan"]["planned_runs"], "readiness": readiness}


def check_attempt(freeze_dir: Path, context: Mapping) -> dict:
    """Verify actual inputs immediately before creating a paid attempt client.

    ``planned`` is the current original schedule row, not a filtered/reindexed
    schedule. Input dictionaries must be the actual TaskFixture model dump.
    Current YAML is not reloaded: the actual canonical fixture is checked.
    An opaque client factory's configuration can only be checked when the caller
    explicitly supplies public ``model_config``; this limit is reported.
    """
    verified = verify_experiment(freeze_dir)
    data = _read_json(Path(freeze_dir).resolve() / "freeze.json")
    if not isinstance(context, Mapping) or context.get("phase") not in {"warmup", "heldout"}:
        raise ValueError("attempt guard requires warmup or heldout context")
    if context.get("workspace_backend") != data["settings"]["workspace_backend"]:
        raise ValueError("actual workspace backend differs from freeze")
    deadline = context.get("hard_timeout_s")
    if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
            or deadline != data["settings"]["hard_timeout_s"]):
        raise ValueError("actual hard deadline differs from freeze")
    row = context.get("planned")
    if not isinstance(row, Mapping) or type(row.get("repeat")) is not int:
        raise ValueError("invalid actual attempt identity")
    matches = [item for item in data["plan"]["schedule"]
               if all(item[key] == row.get(key) for key in ("fixture_id", "repeat", "group"))]
    if len(matches) != 1:
        raise ValueError("actual attempt is outside the frozen schedule")
    expected = matches[0]
    for key in ("memory_enabled", "max_slots", "role_review_enabled"):
        if type(row.get(key)) is not type(expected[key]) or row.get(key) != expected[key]:
            raise ValueError("actual group controls differ from freeze")
    for key in ("fixture_index", "group_index", "relative_dir"):
        if key in row and row[key] != expected[key]:
            raise ValueError("actual attempt path/index differs from freeze")
    if expected["eligibility"] != "ready":
        raise ValueError("attempt is blocked by a missing external capability")
    fixture = context.get("fixture")
    if isinstance(fixture, TaskFixture):
        fixture = fixture.model_dump(mode="json")
    frozen = next(item for item in data["fixtures"] if item["id"] == expected["fixture_id"])
    if not isinstance(fixture, dict) or _json_bytes(fixture) != _json_bytes(frozen):
        raise ValueError("actual fixture or budget differs from the frozen specification")
    if context["phase"] == "warmup" and frozen.get("warmup") is None:
        raise ValueError("attempt has no frozen warmup specification")
    model_checked = "model_config" in context
    if model_checked and _model_config(context["model_config"]) != data["model_config"]:
        raise ValueError("actual public model configuration differs from freeze")
    return {"valid": True, "experiment_id": verified["experiment_id"],
            "freeze_sha256": verified["freeze_sha256"], "phase": context["phase"],
            "fixture_id": expected["fixture_id"], "repeat": expected["repeat"],
            "group": expected["group"], "model_config_checked": model_checked,
            "model_calls": 0, "started": False}


def check_start(freeze_dir: Path, *, scope: str = "full") -> dict:
    """Return a decision only. Never start a client, mutate a plan, or fake resume."""
    if scope not in {"full", "non-network"}:
        raise ValueError("start scope must be full or non-network")
    verified = verify_experiment(freeze_dir)
    data = _read_json(Path(freeze_dir).resolve() / "freeze.json")
    eligible = [row for row in data["plan"]["schedule"] if row["eligibility"] == "ready"]
    reasons = []
    if sys.platform != "win32":
        reasons.append("native_windows_required_for_formal_hard_workers")
    if scope == "full" and not data["readiness"]["full_run_ready"]:
        reasons.append("required_capability_missing")
    if scope == "non-network":
        eligible = [row for row in eligible if "web_search" not in row["required_capabilities"]]
    if not eligible:
        reasons.append("no_eligible_attempts")
    return {"experiment_id": verified["experiment_id"], "scope": scope,
            "ready": not reasons, "reason_codes": reasons,
            "planned_runs": data["plan"]["planned_runs"],
            "selected_count": len(eligible) if not reasons else 0,
            "eligible_count": len(eligible),
            "blocked_count": data["readiness"]["blocked_count"],
            "deferred_count": data["plan"]["planned_runs"] - len(eligible),
            "selected_jobs": eligible if not reasons else [], "model_calls": 0,
            "started": False, "execution_implemented_here": False,
            "full_experiment_complete": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Freeze and verify experiments without model calls"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--tasks", default="eval/tasks")
    prepare.add_argument("--out", required=True)
    prepare.add_argument("--model-config", required=True,
                         help="JSON containing public model fields")
    prepare.add_argument("--capabilities", help="JSON containing boolean capability flags only")
    prepare.add_argument("--repeats", type=int, default=3)
    prepare.add_argument("--workspace-backend", choices=BACKENDS, default="local")
    prepare.add_argument("--hard-timeout", type=float, default=180)
    for prefix in ("", "warmup-"):
        for name in ("steps", "tokens", "wall-s", "retries"):
            prepare.add_argument(f"--{prefix}max-{name}", type=int, default=None)
    verify = commands.add_parser("verify")
    verify.add_argument("directory")
    verify.add_argument("--archive-only", action="store_true")
    start = commands.add_parser("check-start")
    start.add_argument("directory")
    start.add_argument("--scope", choices=("full", "non-network"), default="full")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            def overrides(prefix):
                return {f"max_{name}": getattr(args, f"{prefix}max_{name}")
                        for name in ("steps", "tokens", "wall_s", "retries")
                        if getattr(args, f"{prefix}max_{name}") is not None}

            data = prepare_experiment(
                load_tasks(args.tasks), Path(args.out), repeats=args.repeats,
                model_config=_read_json(Path(args.model_config)),
                capabilities=_read_json(Path(args.capabilities)) if args.capabilities else {},
                workspace_backend=args.workspace_backend, hard_timeout_s=args.hard_timeout,
                budget_override=overrides(""), warmup_budget_override=overrides("warmup_"),
            )
            output = {"experiment_id": data["experiment_id"], "status": data["status"],
                      "readiness": data["readiness"], "model_calls": 0}
        elif args.command == "verify":
            output = verify_experiment(args.directory, check_current=not args.archive_only)
        else:
            output = check_start(args.directory, scope=args.scope)
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 2 if args.command == "check-start" and not output["ready"] else 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"experiment freeze failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
