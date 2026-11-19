"""A UI client for the existing P3 file-HumanIO transport.

The backend owns consumption, callbacks, question recovery and quality. This
module only inspects saved evidence and stages a person's explicit response.
Paths are checked at the application boundary; the run directory must be trusted
against adversarial concurrent directory replacement, not treated as an OS jail.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PureWindowsPath
from tempfile import NamedTemporaryFile
from typing import TypeVar

from moumou.contracts import HumanRequest, HumanResponse

_ID = re.compile(r"[A-Za-z0-9_-]{1,120}")
_SCENARIO = re.compile(r"scenario_[1-9][0-9]*")
_PHASES = {"task_1", "task_2"}
_JSON_LIMIT = 32 * 1024 * 1024
_REQUEST_LIMIT = 1024 * 1024
_READ_RETRY_DELAYS = (0.01, 0.03)
_T = TypeVar("_T")


class SessionNotReady(RuntimeError):
    """A saved index has not caught up with a normal backend transition yet."""


class SessionReadError(OSError):
    """A bounded read retry was exhausted; no transport state was inferred."""


class _WindowsPathRace(OSError):
    """resolve returned only an extended-prefix spelling; do not use that result."""


class StaleRequest(ValueError):
    """The displayed question is no longer the current pending question."""


@dataclass(frozen=True)
class SessionContext:
    scenario_id: str
    phase: str
    conversation_id: str = "instructor-group"


@dataclass(frozen=True)
class PendingRequest:
    context: SessionContext
    request_id: str
    request: HumanRequest
    archive_sha256: str
    pending_sha256: str


@dataclass(frozen=True)
class StageResult:
    request_id: str
    state: str
    path: str | None = None


@dataclass(frozen=True)
class HistoryEvent:
    source: str
    line_number: int
    record: dict


def _is_alias(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate transport field: {key}")
        result[key] = value
    return result


def _invalid(value):
    raise ValueError(f"non-finite transport value: {value}")


def _decode(raw: bytes) -> dict:
    value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique,
                       parse_constant=_invalid)
    if not isinstance(value, dict):
        raise ValueError("a transport record must be a JSON object")
    return value


def validate_response(request: HumanRequest, response: HumanResponse) -> HumanResponse:
    """Validate the person's exact decision; never manufacture a default answer."""
    parsed = HumanResponse.model_validate(response)
    if parsed.request_id != request.id:
        raise ValueError("response request_id does not match the original request")
    if request.kind == "question":
        if parsed.decision is not None:
            raise ValueError("a clarification answer is not a tool approval")
        if parsed.text is None or not parsed.text.strip():
            raise ValueError("a question requires a non-empty answer")
        if request.options and parsed.text not in request.options:
            raise ValueError("answer must match one of the original options")
    else:
        if parsed.decision not in {"approve", "reject"}:
            raise ValueError("approval/review requires explicit approve or reject")
        if request.options and parsed.decision not in request.options:
            raise ValueError("decision is outside the original options")
    return parsed


class FileSession:
    """Attach to a saved formal RUN launched with ``--human files``.

    Attaching never starts/resumes the Engine and never loads credentials. A saved
    running/pending marker is not proof that its original process is still alive.
    """

    def __init__(self, root: Path | str):
        supplied = Path(root).expanduser()
        if ".." in supplied.parts:
            raise ValueError("RUN must not contain parent traversal")
        absolute = supplied.absolute()
        if _is_alias(absolute) or not absolute.is_dir():
            raise ValueError("RUN must be an existing regular directory")
        self.root = absolute.resolve(strict=True)
        if self.root != absolute:
            raise ValueError("RUN must not be reached through a directory alias")
        self.read_recoveries: deque[dict] = deque(maxlen=20)
        self.manifest()

    def _validate_path(self, path: Path) -> Path:
        try:
            parts = path.relative_to(self.root).parts
        except ValueError as exc:
            raise ValueError("session evidence escapes RUN") from exc
        checked = self.root
        for part in parts:
            checked = checked / part
            if _is_alias(checked):
                raise ValueError("session evidence cannot use file/directory aliases")
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root) or resolved != path:
            spelling = str(path)
            extended = ("\\\\?\\UNC\\" + spelling[2:] if spelling.startswith("\\\\")
                        else "\\\\?\\" + spelling)
            if os.name == "nt" and str(resolved) == extended:
                # ntpath may retain this spelling when its second metadata probe
                # races a replacement/deletion. Never strip it and proceed: the
                # next attempt must independently pass every ordinary check.
                raise _WindowsPathRace(f"lexical={path!s}; resolved={resolved!s}")
            raise ValueError(
                f"session evidence escapes RUN or uses a path alias: {path!s} -> {resolved!s}"
            )
        return path

    def _observe(self, path: Path, operation: str, read: Callable[[Path], _T]) -> _T:
        """Retry only known read races, revalidating all boundaries each time."""
        for attempt in range(len(_READ_RETRY_DELAYS) + 1):
            try:
                result = read(self._validate_path(path))
            except (FileNotFoundError, PermissionError, _WindowsPathRace) as exc:
                if attempt < len(_READ_RETRY_DELAYS):
                    last_error = exc
                    time.sleep(_READ_RETRY_DELAYS[attempt])
                    continue
                message = (
                    f"session read failed: operation={operation}; "
                    f"path={path.relative_to(self.root).as_posix()}; attempts={attempt + 1}; "
                    f"error={type(exc).__name__}; errno={exc.errno}; "
                    f"winerror={getattr(exc, 'winerror', None)}; {exc}; "
                    "no state or decision inferred"
                )
                if isinstance(exc, FileNotFoundError):
                    # Optional pending/response files may have been consumed.
                    # Required metadata instead surfaces the bounded failure.
                    raise FileNotFoundError(exc.errno, message, str(path)) from exc
                raise SessionReadError(message) from exc
            if attempt:
                self.read_recoveries.append({
                    "operation": operation, "path": path.relative_to(self.root).as_posix(),
                    "attempts": attempt + 1, "error": type(last_error).__name__,
                    "errno": last_error.errno, "winerror": getattr(last_error, "winerror", None),
                    "detail": str(last_error),
                })
            return result
        raise AssertionError("bounded session read loop did not return")

    def _path(self, *parts: str) -> Path:
        path = self.root
        for part in parts:
            if (not isinstance(part, str) or not part or part in {".", ".."}
                    or any(char in part for char in ("/", "\\", ":", "\0"))
                    or part.endswith((".", " ")) or PureWindowsPath(part).is_reserved()):
                raise ValueError("unsafe session path component")
            path = path / part
        return self._observe(path, "validate path", lambda checked: checked)

    def _exists(self, path: Path, *, directory=False) -> bool:
        return self._observe(path, "inspect presence", lambda checked: (
            checked.is_dir() if directory else checked.exists()
        ))

    def _read(self, path: Path, limit: int = _JSON_LIMIT, *, inflight=False) -> tuple[dict, str]:
        def read(checked):
            info = checked.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError("invalid or oversized session evidence file")
            raw = checked.read_bytes()
            if len(raw) > limit:
                raise ValueError("session evidence grew beyond its size bound")
            return raw

        raw = self._observe(path, "read JSON", read)
        try:
            value = _decode(raw)
        except (json.JSONDecodeError, UnicodeError) as exc:
            if inflight and not raw.endswith(b"\n"):
                raise SessionNotReady(
                    f"{path.name} is incomplete or still being published; retry, and inspect "
                    "the original record if this persists (no decision inferred)"
                ) from exc
            raise
        return value, hashlib.sha256(raw).hexdigest()

    def manifest(self) -> dict:
        value, _ = self._read(self._path("test.json"))
        if (type(value.get("schema_version")) is not int or value["schema_version"] != 1
                or value.get("kind") != "instructor_test"
                or not isinstance(value.get("dataset_kind"), str)
                or value.get("dataset_kind") not in {"instructor", "supplementary"}):
            raise ValueError("--run requires the current formal test.json protocol")
        if value.get("human_mode") != "files":
            raise ValueError("attach requires a backend launched with --human files")
        scenarios = value.get("scenarios")
        if not isinstance(scenarios, list):
            raise ValueError("session scenarios must be a list")
        ids = []
        for scenario in scenarios:
            if (not isinstance(scenario, dict)
                    or not _SCENARIO.fullmatch(str(scenario.get("scenario_id", "")))):
                raise ValueError("invalid saved scenario identity")
            ids.append(scenario["scenario_id"])
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate saved scenario identity")
        return value

    def current(self) -> SessionContext:
        manifest = self.manifest()
        active_path = self._path("active.json")
        if not self._exists(active_path):
            raise SessionNotReady("active.json is not saved yet; waiting for backend metadata")
        active, _ = self._read(active_path, _REQUEST_LIMIT)
        scenario_id, phase = active.get("scenario_id"), active.get("phase")
        if not isinstance(scenario_id, str) or not _SCENARIO.fullmatch(scenario_id):
            raise ValueError("invalid active scenario identity")
        if not isinstance(phase, str) or phase not in _PHASES:
            raise ValueError("invalid active task phase")
        if scenario_id not in {item["scenario_id"] for item in manifest["scenarios"]}:
            raise SessionNotReady("active scenario is not indexed in test.json yet")
        folder = self._path(scenario_id)
        expected = {"scenario_root": folder,
                    "human_pending": self._path(scenario_id, "human", "pending"),
                    "manifest": self._path("test.json")}
        for field, path in expected.items():
            raw = active.get(field)
            if (not isinstance(raw, str) or not Path(raw).is_absolute()
                    or ".." in Path(raw).parts or Path(raw).absolute() != path):
                raise ValueError(f"active.{field} disagrees with the selected RUN")
            self._observe(Path(raw), f"validate active.{field}", lambda checked: checked)
        return SessionContext(scenario_id, phase)

    def _record(self, context: SessionContext, request_id: str) -> PendingRequest | None:
        if not _ID.fullmatch(request_id):
            raise ValueError("unsafe original request ID")
        prefix = (context.scenario_id, "human")
        pending = self._path(*prefix, "pending", request_id + ".json")
        if not self._exists(pending):
            return None
        original, archive_hash = self._read(
            self._path(*prefix, "requests", request_id + ".json"), _REQUEST_LIMIT,
        )
        try:
            record, pending_hash = self._read(pending, _REQUEST_LIMIT, inflight=True)
        except FileNotFoundError:
            return None
        if record != original:
            raise ValueError("pending request differs from its original archived record")
        if record.get("phase") != context.phase:
            return None
        created = record.get("created_at")
        if not isinstance(created, str) or datetime.fromisoformat(created).utcoffset() is None:
            raise ValueError("request record needs its original aware creation timestamp")
        request = HumanRequest.model_validate(record.get("request"))
        if request.model_dump(mode="json") != record.get("request"):
            raise ValueError("request body is incomplete or has unknown fields")
        if request.id != request_id or not request.task_id or not request.agent_id:
            raise ValueError("request path/body identity mismatch")
        if request.project_id and request.project_id != context.scenario_id:
            raise ValueError("request belongs to another scenario")
        if request.conversation_id and request.conversation_id != context.conversation_id:
            raise ValueError("request belongs to another conversation")
        if request.kind == "question" and not (
            request.project_id and request.conversation_id and request.role
        ):
            raise ValueError("formal clarification requires its original explicit scope")
        return PendingRequest(context, request_id, request, archive_hash, pending_hash)

    def _state(self, item: PendingRequest) -> str:
        for directory, state in (("consumed", "already_consumed"),
                                 ("responses", "already_staged")):
            path = self._path(item.context.scenario_id, "human", directory,
                              item.request_id + ".json")
            if self._exists(path):
                try:
                    response, _ = self._read(path, _REQUEST_LIMIT, inflight=True)
                except FileNotFoundError:
                    continue
                validate_response(item.request, HumanResponse.model_validate(response))
                return state
        return "pending"

    def pending(self) -> list[PendingRequest]:
        context = self.current()
        directory = self._path(context.scenario_id, "human", "pending")
        if not self._exists(directory, directory=True):
            raise SessionNotReady("the active human queue has not been created yet")
        requests = []
        paths = self._observe(
            directory, "list pending", lambda checked: sorted(checked.iterdir()),
        )
        for path in paths:
            if path.suffix != ".json":
                continue
            item = self._record(context, path.stem)
            if item is not None and self._state(item) == "pending":
                requests.append(item)
        return requests

    def _recheck(self, item: PendingRequest) -> tuple[PendingRequest | None, str]:
        if self.current() != item.context:
            raise StaleRequest("active scenario/phase changed; inspect the current queue again")
        state = self._state(item)
        if state != "pending":
            return None, state
        fresh = self._record(item.context, item.request_id)
        if fresh is None:
            return None, "no_longer_pending"
        if (fresh.archive_sha256 != item.archive_sha256
                or fresh.pending_sha256 != item.pending_sha256
                or fresh.request.model_dump(mode="json") != item.request.model_dump(mode="json")):
            raise StaleRequest("the original displayed request changed; no response was published")
        return fresh, "pending"

    def stage_response(self, item: PendingRequest, response: HumanResponse) -> StageResult:
        fresh, state = self._recheck(item)
        if fresh is None:
            return StageResult(item.request_id, state)
        answer = validate_response(fresh.request, response)
        content = (json.dumps(answer.model_dump(mode="json"), ensure_ascii=False,
                              allow_nan=False, indent=2) + "\n").encode("utf-8")
        if len(content) > _REQUEST_LIMIT:
            raise ValueError("response exceeds the existing backend transport size bound")
        directory = self._path(item.context.scenario_id, "human", "responses")
        if not self._exists(directory, directory=True):
            raise ValueError("the backend response directory is missing")
        target = self._path(item.context.scenario_id, "human", "responses",
                            item.request_id + ".json")
        temporary = None
        try:
            with NamedTemporaryFile(mode="wb", dir=directory,
                                    prefix=".p2-response-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            _, state = self._recheck(item)
            if state != "pending":
                return StageResult(item.request_id, state)
            # The existing FileHuman watches for the destination's existence.
            # A hard link publishes complete bytes atomically without replacing a
            # response from another UI; open('x') followed by writes is not atomic.
            try:
                os.link(temporary, target)
            except FileExistsError:
                state = self._state(item)
                return StageResult(
                    item.request_id,
                    state if state != "pending" else "no_longer_pending", str(target),
                )
            except OSError as exc:
                raise OSError(
                    "atomic no-clobber response publication is unavailable on this filesystem; "
                    "no replacement fallback was attempted"
                ) from exc
            try:
                observed = self._state(item)
            except SessionNotReady:
                return StageResult(item.request_id, "staged_backend_record_incomplete", str(target))
            if observed == "already_consumed":
                # If the backend consumed another UI's response between our final
                # check and publication, remove only the link that this UI owns.
                try:
                    ours, current = temporary.stat(), target.stat()
                    if (ours.st_dev, ours.st_ino) == (current.st_dev, current.st_ino):
                        target.unlink()
                except FileNotFoundError:
                    pass
                return StageResult(item.request_id, "already_consumed")
            return StageResult(item.request_id, "staged", str(target))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def history(self, limit: int = 30) -> list[HistoryEvent]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("history limit must be between 1 and 200")
        context = self.current()
        result = []

        def read(checked):
            if not checked.is_file() or checked.stat().st_size > 256 * 1024 * 1024:
                raise ValueError("history file is invalid or exceeds its UI read bound")
            rows = deque(maxlen=limit)
            with checked.open("rb") as stream:
                for number, raw in enumerate(stream, 1):
                    if len(raw) > _JSON_LIMIT:
                        raise ValueError("history line exceeds its size bound")
                    if raw.strip():
                        try:
                            record = _decode(raw)
                        except (json.JSONDecodeError, UnicodeError):
                            if not raw.endswith(b"\n"):
                                # The backend may be appending this last row.
                                # A completed malformed row is still an error.
                                break
                            raise
                        rows.append((number, record))
            return rows

        for parts in ((context.scenario_id, "dialogue.jsonl"),
                      (context.scenario_id, "human", "human-events.jsonl"),
                      (context.scenario_id, "trace.jsonl")):
            path = self._path(*parts)
            if not self._exists(path):
                continue
            try:
                rows = self._observe(path, "read history", read)
            except FileNotFoundError:
                continue
            result.extend(HistoryEvent(path.relative_to(self.root).as_posix(), number, record)
                          for number, record in rows)
        return result
