"""Bind lexical source checks to bytes read from an actual private-workspace artifact."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import stat
from collections import deque
from pathlib import Path, PureWindowsPath
from uuid import uuid4

from moumou.contracts import ToolResult
from moumou.eval.source_evidence import digest, timestamp
from moumou.sandbox.local import LocalJail
from moumou.tools.source_quote import VerifySourceQuoteTool

_HASH = re.compile(r"[0-9a-f]{64}")
_MAX_FILE_BYTES = 1024 * 1024
_MAX_QUOTE_CHARS = 4096
_MAX_QUOTE_BYTES = 16384
_MAX_OUTPUT_BYTES = 16384
_MAX_AUDIT_RECORDS = 256
_PRIVATE_PARTS = {".git", ".evaluation", ".aws", ".ssh", ".codex"}
_PRIVATE_NAMES = {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials.json"}
_PRIVATE_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
_LIMITATION = (
    "Lexical presence of the actual artifact slice only, not paraphrase fidelity, truth, "
    "correct attribution, metric/date scope, task success or human approval. A non-match "
    "does not prove a claim false; unknown is unavailable evidence. This receipt applies "
    "only to its artifact SHA256 at the checks; later exports must match that full hash. "
    "Application path checks are not an OS sandbox or protection from a hostile OS writer."
)


class _Rejected(ValueError):
    pass


class _Changed(ValueError):
    pass


def _canonical(path):
    absolute = Path(path).absolute()
    if str(absolute).startswith(("\\\\", "//")):
        raise _Rejected("artifact_alias_refused")
    for item in (absolute, *absolute.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise _Rejected("artifact_alias_refused")
    if absolute.resolve(strict=True) != absolute:
        raise _Rejected("artifact_alias_refused")
    return absolute


def _relative_path(value):
    if (not isinstance(value, str) or not value or "\0" in value
            or Path(value).is_absolute() or PureWindowsPath(value).anchor):
        raise _Rejected("invalid_artifact_path")
    try:
        if len(value.encode("utf-8")) > 2048:
            raise _Rejected("invalid_artifact_path")
    except UnicodeError as exc:
        raise _Rejected("invalid_artifact_path") from exc
    parts = value.replace("\\", "/").split("/")
    for part in parts:
        if (part in {"", ".", ".."} or ":" in part or part.endswith((".", " "))
                or PureWindowsPath(part).is_reserved()):
            raise _Rejected("invalid_artifact_path")
        name = part.casefold()
        if (name in _PRIVATE_PARTS or name in _PRIVATE_NAMES or name.startswith(".env")
                or Path(name).suffix in _PRIVATE_SUFFIXES):
            raise _Rejected("private_artifact_path_refused")
    return Path(*parts)


def _identity(info):
    # Native Windows path.stat and fstat can report different ctime semantics.
    # Compare creation time across APIs when available; retain each API's ctime
    # separately in _state so a change is not silently ignored.
    creation = getattr(info, "st_birthtime_ns", None) if os.name == "nt" else info.st_ctime_ns
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_mode, info.st_nlink, creation)


def _state(info):
    return _identity(info), info.st_ctime_ns


def _snapshot(root, relative):
    """Two bounded reads of one regular handle, with path/identity checks on both sides."""
    _canonical(root)
    path = _canonical(root / relative)
    if LocalJail(root).resolve(relative.as_posix()) != path:
        raise _Rejected("artifact_path_outside_workspace")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise _Rejected("artifact_must_be_single_link_regular_file")
    if before.st_size > _MAX_FILE_BYTES:
        raise ValueError("artifact_exceeds_size_bound")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        _canonical(path)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or _identity(opened) != _identity(before)):
            raise _Changed("artifact_changed_during_read")
        raw = stream.read(_MAX_FILE_BYTES + 1)
        stream.seek(0)
        again = stream.read(_MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    _canonical(root)
    _canonical(path)
    final = path.stat()
    if (len(raw) > _MAX_FILE_BYTES or raw != again or len(raw) != before.st_size
            or _state(before) != _state(final) or _state(opened) != _state(after)
            or _identity(after) != _identity(final)):
        raise _Changed("artifact_changed_during_read")
    return raw, (_state(final), _state(after))


class VerifyArtifactQuoteTool:
    name = "verify_artifact_quote"
    description = (
        "Read a literal slice of an actual UTF-8 artifact in your own workspace and check "
        "it against exact retained source metadata. start/end are Unicode code-point offsets, "
        "zero-based [start,end). No quote text is accepted. Return file/slice hashes; later "
        "rewrites invalidate applicability. Lexical matching is not truth or approval."
    )
    action_class = "fs_read"
    timeout_s = 10.0
    params_schema = {"type": "object", "additionalProperties": False, "properties": {
        "metadata_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "path": {"type": "string", "minLength": 1, "maxLength": 2048},
        "start": {"type": "integer", "minimum": 0},
        "end": {"type": "integer", "minimum": 1},
        "mode": {"type": "string", "enum": ["exact", "collapse_whitespace"], "default": "exact"},
        "expected_artifact_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    }, "required": ["metadata_sha256", "path", "start", "end"]}

    def __init__(self, store, *, workspaces=None):
        self._source = VerifySourceQuoteTool(store)
        self._workspaces = None
        self._audit = deque(maxlen=_MAX_AUDIT_RECORDS)
        self._audit_dropped = 0
        if workspaces is not None:
            self.bind_workspaces(workspaces)

    def bind_workspaces(self, workspaces):
        """Trusted operator only: bind actual workspace-provider paths exactly once."""
        if self._workspaces is not None or isinstance(workspaces, (str, bytes, Path)):
            raise ValueError("artifact quote requires an unbound collection of actual workspaces")
        paths = frozenset(_canonical(path) for path in workspaces)
        if not paths or any(not path.is_dir() for path in paths):
            raise ValueError("artifact quote requires existing actual workspace directories")
        self._source.bind_workspaces(paths)
        self._workspaces = paths

    def audit_records(self):
        """Return independent copies; only tool-owned records are verification evidence."""
        return {"schema_version": 1, "records": copy.deepcopy(list(self._audit)),
                "dropped_records": self._audit_dropped}

    @property
    def audit_dropped_records(self):
        return self._audit_dropped

    def _finish(self, payload, ctx, *, ok=False):
        payload = {**payload, "schema_version": 1, "tool": self.name,
                   "check_id": uuid4().hex, "checked_at": timestamp(), "limitation": _LIMITATION}
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > _MAX_OUTPUT_BYTES:
            payload = {"status": "unknown", "lexical_match": None,
                       "reason": "bounded_receipt_unavailable", "schema_version": 1,
                       "tool": self.name, "check_id": payload["check_id"],
                       "checked_at": payload["checked_at"], "limitation": _LIMITATION}
            encoded, ok = json.dumps(payload, ensure_ascii=False), False
        source_check = {key: value for key, value in payload.items()
                        if key not in {"artifact", "check_id", "checked_at", "tool",
                                       "schema_version", "limitation"}}
        record = {"check_id": payload["check_id"], "checked_at": payload["checked_at"],
                  "agent_id": str(getattr(ctx, "agent_id", ""))[:256],
                  "task_id": str(getattr(ctx, "task_id", ""))[:256],
                  "artifact": payload.get("artifact"), "source_check": source_check}
        if len(self._audit) == _MAX_AUDIT_RECORDS:
            self._audit_dropped += 1
        self._audit.append(copy.deepcopy(record))
        error = None if ok else "artifact quote unavailable: " + payload.get(
            "reason", "source_unavailable",
        )
        return ToolResult(ok=ok, stdout=encoded, error=error)

    def _failure(self, reason, ctx, *, rejected=False, artifact=None):
        payload = {"status": "rejected" if rejected else "unknown", "lexical_match": None,
                   "reason": reason}
        if artifact is not None:
            payload["artifact"] = artifact
        return self._finish(payload, ctx)

    async def run(self, args, ctx):
        artifact = None
        try:
            allowed = {"metadata_sha256", "path", "start", "end", "mode",
                       "expected_artifact_sha256"}
            if not isinstance(args, dict) or set(args) - allowed:
                raise _Rejected("invalid_arguments")
            key, start, end = args.get("metadata_sha256"), args.get("start"), args.get("end")
            mode, expected = args.get("mode", "exact"), args.get("expected_artifact_sha256")
            if (not isinstance(key, str) or not _HASH.fullmatch(key)
                    or type(start) is not int or type(end) is not int
                    or start < 0 or end <= start or end - start > _MAX_QUOTE_CHARS
                    or not isinstance(mode, str) or mode not in {"exact", "collapse_whitespace"}
                    or ("expected_artifact_sha256" in args and (
                        not isinstance(expected, str) or not _HASH.fullmatch(expected)))):
                raise _Rejected("invalid_arguments")
            relative = _relative_path(args.get("path"))
            if self._workspaces is None:
                raise _Rejected("workspace_binding_unavailable")
            if (not Path(ctx.workspace).is_absolute()
                    or _canonical(ctx.workspace) not in self._workspaces):
                raise _Rejected("outside_bound_workspace_scope")
            root = Path(ctx.workspace).absolute()
            raw, identity = await asyncio.to_thread(_snapshot, root, relative)
            text = raw.decode("utf-8")
            if end > len(text):
                raise _Rejected("artifact_span_out_of_bounds")
            quote = text[start:end]
            if not quote.strip() or len(quote.encode("utf-8")) > _MAX_QUOTE_BYTES:
                raise _Rejected("invalid_artifact_span")
            artifact = {"path": relative.as_posix(), "sha256": digest(raw), "bytes": len(raw),
                        "characters": len(text), "quote_sha256": digest(quote.encode("utf-8")),
                        "span": {"space": "UTF-8 decoded artifact", "unit": "Unicode code points",
                                 "interval": "zero-based [start,end)", "start": start, "end": end,
                                 "byte_start": len(text[:start].encode("utf-8")),
                                 "byte_end": len(text[:end].encode("utf-8"))},
                        "stable_at_checks": False}
            if expected is not None and expected != artifact["sha256"]:
                raise _Rejected("artifact_sha256_mismatch")
            source_result = await self._source.run(
                {"metadata_sha256": key, "quote": quote, "mode": mode}, ctx,
            )
            checked_raw, checked_identity = await asyncio.to_thread(_snapshot, root, relative)
            if checked_raw != raw or checked_identity != identity:
                raise _Changed("artifact_changed_during_verification")
            artifact["stable_at_checks"] = True
            source = json.loads(source_result.stdout)
            # Never journal artifact/source prose. Keep hashes, coordinates and source identity.
            source.pop("evidence_excerpt", None)
            source.pop("checked_at", None)
            source.pop("limitation", None)
            return self._finish({**source, "artifact": artifact}, ctx, ok=source_result.ok)
        except _Rejected as exc:
            return self._failure(str(exc), ctx, rejected=True, artifact=artifact)
        except _Changed as exc:
            return self._failure(str(exc), ctx, artifact=artifact)
        except UnicodeError:
            return self._failure("artifact_not_valid_utf8", ctx, artifact=artifact)
        except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError):
            return self._failure("artifact_or_source_unavailable", ctx, artifact=artifact)
