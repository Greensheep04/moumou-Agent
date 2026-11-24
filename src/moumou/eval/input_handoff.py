"""Explicit teacher-plan inputs and first-admission file checks, never a quality grade."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime

from moumou.contracts import TaskNode, TaskResult
from moumou.tools.artifact_quote import _canonical, _relative_path, _snapshot
from moumou.tools.registry import ToolRegistry

_MAX_INPUTS = 32
_MAX_RECORD_BYTES = 128 * 1024
_INDEX_CHARS = 3500
_STATIC_FIELDS = ("id", "project_id", "conversation_id", "parent_id", "role", "goal")


class InputHandoffError(ValueError):
    """A declared input was not delivered; no model call or quality approval follows."""

    def __init__(self, message, *, detail=None):
        super().__init__(message)
        self.detail = detail or {}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _declared_paths(source):
    # ``source.artifacts`` is now the published manifest, so a long task with
    # many tool writes no longer zeroes out declared input checking.
    if not isinstance(source.result, TaskResult) or len(source.result.files) > 256:
        return set()
    if not isinstance(source.artifacts, list) or len(source.artifacts) > 256:
        return set()
    groups = []
    for values in (source.result.files, source.artifacts):
        paths = set()
        for value in values:
            try:
                paths.add(_relative_path(value).as_posix())
            except (OSError, TypeError, ValueError, RuntimeError):
                pass
        groups.append(paths)
    return groups[0] & groups[1]


def install_input_schema(engine):
    """Replace only this Engine's marker schema, never mutate shared/default tools."""
    marker = engine.tools.get("submit_plan")
    if marker is None:
        raise ValueError("declared inputs require submit_plan")
    replacement = copy.copy(marker)
    schema = copy.deepcopy(marker.params_schema)
    item = schema["properties"]["subtasks"]["items"]
    item["properties"]["inputs"] = {
        "type": "array", "maxItems": _MAX_INPUTS,
        "description": (
            "Every required file from another producer: producer must match an entry in deps "
            "(new sibling key, retained direct child ID, or authorized inherited ID). Path is "
            "its relative declared output. Supply [] only when no external file is required. "
            "Parent workspace files are not automatically delivered."
        ),
        "items": {"type": "object", "additionalProperties": False, "properties": {
            "producer": {
                "type": "string", "pattern": r"^(?:[a-z][a-z0-9_]{0,15}|t[0-9]+"
                                             r"(?:\.[a-z][a-z0-9_]*)*)$",
            },
            "path": {"type": "string", "minLength": 1, "maxLength": 2048},
        }, "required": ["producer", "path"]},
    }
    item["required"] = list(dict.fromkeys([*item.get("required", []), "inputs"]))
    replacement.params_schema = schema
    registry = ToolRegistry()
    for name in sorted(engine.tools.names()):
        registry.register(replacement if name == "submit_plan" else engine.tools.get(name))
    engine.tools = engine.runner.tools = engine.gate.tools = registry


class DeclaredInputs:
    """Native DB metadata scoped to one actual teacher team, not model-authored history.

    Checks only explicitly declared inputs at first admission. Omitted declarations
    cannot be inferred from prose. Existing native artifact transfer semantics and
    final-file review remain separate; this is not an immutable producer snapshot.
    """

    def __init__(self, repo, workspaces, project_id, conversation_id, trace,
                 read_publication=None):
        self.repo, self.db, self.trace = repo, repo.db, trace
        self.project, self.conversation = project_id, conversation_id
        self.read_publication = read_publication
        self.bind_workspaces(workspaces)

    def bind_workspaces(self, workspaces):
        paths = {role: _canonical(path) for role, path in workspaces.items()}
        if not paths or len(paths) > 16:
            raise ValueError("invalid declared-input team")
        for left in paths.values():
            if not left.is_dir() or any(
                left != right and (left.is_relative_to(right) or right.is_relative_to(left))
                for right in paths.values()
            ):
                raise ValueError("invalid declared-input workspace binding")
        if len(set(paths.values())) != len(paths):
            raise ValueError("duplicate declared-input workspaces")
        self.paths = paths

    def _owned(self, node):
        actual = self.repo.get(node.id)
        if (not isinstance(actual, TaskNode) or node.project_id != self.project
                or node.conversation_id != self.conversation or node.role not in self.paths
                or any(getattr(actual, key) != getattr(node, key) for key in _STATIC_FIELDS)):
            raise InputHandoffError("input task is outside the bound team or current repository")
        return actual

    def _key(self, kind, task_id):
        scope = _json([self.project, self.conversation, task_id]).encode("utf-8")
        return "teacher_inputs:" + kind + ":" + _sha(scope)

    def _put(self, kind, task_id, value):
        raw = _json(value)
        if len(raw.encode("utf-8")) > _MAX_RECORD_BYTES:
            raise InputHandoffError("declared input metadata exceeds its bound")
        self.db.set_meta(self._key(kind, task_id), raw)

    def _get(self, kind, node):
        raw = self.db.get_meta(self._key(kind, node.id))
        if raw is None:
            return None
        if len(raw.encode("utf-8")) > _MAX_RECORD_BYTES:
            raise InputHandoffError("declared input metadata is unavailable")
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise InputHandoffError("declared input metadata is unavailable") from exc

    def _delivery_key(self, role, path):
        return "teacher_inputs:last_delivery:" + _sha(
            _json([self.project, self.conversation, role, path]).encode("utf-8"),
        )

    def _delivery_baseline(self, role, path):
        raw = self.db.get_meta(self._delivery_key(role, path))
        if raw is None:
            return None
        try:
            if len(raw.encode("utf-8")) > _MAX_RECORD_BYTES:
                raise ValueError
            value = json.loads(raw)
            if (not isinstance(value, dict) or value.get("schema_version") != 1
                    or value.get("project_id") != self.project
                    or value.get("conversation_id") != self.conversation
                    or value.get("role") != role or value.get("path") != path
                    or not isinstance(value.get("source_id"), str)
                    or not isinstance(value.get("sha256"), str) or len(value["sha256"]) != 64
                    or any(char not in "0123456789abcdef" for char in value["sha256"])
                    or type(value.get("bytes")) is not int
                    or not 0 <= value["bytes"] <= 1024 * 1024):
                raise ValueError
            owner = self.repo.get(value.get("receipt_task_id"))
            if owner is None or owner.role != role:
                raise ValueError
            revision = value.get("contract_sha256")
            contract = self._contract(owner, revision=revision)
            receipt = (self._received(owner, contract, revision=revision)
                       if contract is not None else None)
            expected = {key: value[key] for key in ("source_id", "path", "sha256", "bytes")}
            if receipt is None or expected not in receipt["observed"]:
                raise ValueError
            return value
        except (ValueError, TypeError) as exc:
            raise InputHandoffError("prior native input baseline is unavailable") from exc

    def validate_plan(self, parent, payload, plan, *, child_ids=None):
        """Validate the entire input declaration before any role/task is created."""
        self._owned(parent)
        prefix = parent.id if parent.wakeups == 0 else f"{parent.id}.r{parent.wakeups}"
        raw_specs = {item["key"]: item for item in payload.get("subtasks", [])}
        ids = child_ids if child_ids is not None else {
            spec.key: f"{prefix}.{spec.key}" for spec in plan.subtasks
        }
        replacements = {spec.replaces: ids[spec.key] for spec in plan.subtasks if spec.replaces}
        contracts = {}
        for spec in plan.subtasks:
            entries = raw_specs[spec.key].get("inputs")
            if not isinstance(entries, list) or len(entries) > _MAX_INPUTS:
                raise InputHandoffError(f"subtask {spec.key!r} must explicitly declare inputs")
            requirements, seen = [], set()
            for entry in entries:
                if not isinstance(entry, dict) or set(entry) != {"producer", "path"}:
                    raise InputHandoffError("each input needs only producer and path")
                producer = entry["producer"]
                if not isinstance(producer, str) or producer not in spec.deps:
                    raise InputHandoffError(
                        f"input producer for {spec.key!r} must match an entry in deps"
                    )
                try:
                    relative = _relative_path(entry["path"]).as_posix()
                except (OSError, TypeError, ValueError, RuntimeError) as exc:
                    raise InputHandoffError("input path is not a public relative artifact") from exc
                identity = relative.casefold()
                if identity in seen:
                    raise InputHandoffError("each input path must name exactly one producer")
                seen.add(identity)
                source_id = ids.get(producer, replacements.get(producer, producer))
                source = self.repo.get(source_id)
                if source is not None and source.status == "DONE" and self.read_publication:
                    publication = self.repo.published_version(source_id)
                    if not publication or not any(
                        item["path"] == relative for item in publication["files"]
                    ):
                        raise InputHandoffError(
                            f"{spec.key} requires {relative} from {source_id}, "
                            "but that completed producer did not publish it"
                        )
                requirements.append({
                    "source_id": source_id,
                    "path": relative,
                })
            task_id = ids[spec.key]
            if self.repo.get(task_id) is not None:
                raise InputHandoffError("planned task ID already exists")
            contracts[task_id] = {
                "schema_version": 1, "project_id": parent.project_id,
                "conversation_id": parent.conversation_id, "task_id": task_id,
                "parent_id": parent.id, "role": spec.role,
                "goal_sha256": _sha(spec.goal.encode("utf-8")), "requirements": requirements,
            }
            if len(_json(contracts[task_id]).encode("utf-8")) > _MAX_RECORD_BYTES:
                raise InputHandoffError("declared input metadata exceeds its bound")
        return contracts

    def persist(self, contracts):
        self.db.conn.execute("SAVEPOINT teacher_input_contracts")
        try:
            for task_id, contract in contracts.items():
                self._put("contract", task_id, contract)
            self.db.conn.execute("RELEASE SAVEPOINT teacher_input_contracts")
        except BaseException:
            self.db.conn.execute("ROLLBACK TO SAVEPOINT teacher_input_contracts")
            self.db.conn.execute("RELEASE SAVEPOINT teacher_input_contracts")
            raise

    def _archive_admission(self, task, contract):
        receipt = self._received(task, contract)
        if receipt is None:
            return
        revision = receipt["contract_sha256"]
        for item in receipt["observed"]:
            path = item["path"]
            baseline = self._delivery_baseline(task.role, path)
            if (baseline is not None and baseline["receipt_task_id"] == task.id
                    and baseline.get("contract_sha256") is None):
                self.db.set_meta(self._delivery_key(task.role, path), _json({
                    **baseline, "contract_sha256": revision,
                }))
        self._put(f"contract:{revision}", task.id, contract)
        self._put(f"received:{revision}", task.id, receipt)
        self._put("received", task.id, None)

    def resume_base(self, task):
        recovery = (task.blocker or {}).get("base_recovery")
        if recovery is None:
            return
        contract = self._contract(task)
        requirements = contract["requirements"]
        additions = [{"source_id": recovery["base_task"], "path": path}
                     for path in recovery["paths"]]
        if {item["path"].casefold() for item in requirements} & {
            item["path"].casefold() for item in additions
        }:
            raise InputHandoffError("resume needs one producer per path. Replan with replaces.")
        if len(requirements) + len(additions) > _MAX_INPUTS:
            raise InputHandoffError("recovered input declaration exceeds its bound")
        self._archive_admission(task, contract)
        self._put("contract", task.id, {**contract, "requirements": requirements + additions})

    def admitted_files(self, task):
        contract = self._contract(task)
        receipt = self._received(task, contract) if contract is not None else None
        return receipt["observed"] if receipt is not None else []

    def replace_producers(self, tasks, replacements):
        for task in tasks:
            contract = self._contract(task)
            requirements = contract["requirements"] if contract else []
            if any(item["source_id"] in replacements for item in requirements):
                self._archive_admission(task, contract)
                for item in requirements:
                    item["source_id"] = replacements.get(item["source_id"], item["source_id"])
                self._put("contract", task.id, contract)

    def _contract(self, node, *, revision=None):
        self._owned(node)
        if node.parent_id is None:
            return None
        key = f"contract:{revision}" if revision is not None else "contract"
        value = self._get(key, node)
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or any(value.get(key) != getattr(node, field) for key, field in (
                    ("project_id", "project_id"), ("conversation_id", "conversation_id"),
                    ("task_id", "id"), ("parent_id", "parent_id"), ("role", "role")))
                or value.get("goal_sha256") != _sha(node.goal.encode("utf-8"))
                or not isinstance(value.get("requirements"), list)
                or len(value["requirements"]) > _MAX_INPUTS
                or (revision is not None and _sha(_json(value).encode("utf-8")) != revision)):
            raise InputHandoffError("required input contract is unavailable or mismatched")
        return value

    @staticmethod
    def _valid_observations(observed, contract):
        if not isinstance(observed, list) or len(observed) != len(contract["requirements"]):
            return False
        for item, requirement in zip(observed, contract["requirements"], strict=True):
            if (not isinstance(item, dict)
                    or set(item) != {"source_id", "path", "sha256", "bytes"}
                    or item["source_id"] != requirement["source_id"]
                    or item["path"] != requirement["path"]
                    or not isinstance(item["sha256"], str) or len(item["sha256"]) != 64
                    or any(char not in "0123456789abcdef" for char in item["sha256"])
                    or type(item["bytes"]) is not int or not 0 <= item["bytes"] <= 1024 * 1024):
                return False
        return True

    def _received(self, node, contract, *, revision=None):
        key = f"received:{revision}" if revision is not None else "received"
        value = self._get(key, node)
        if value is not None and (
            not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("contract_sha256") != _sha(_json(contract).encode("utf-8"))
            or not self._valid_observations(value.get("observed"), contract)
            or value.get("final_file_review") is not None or value.get("human_quality") is not None
        ):
            raise InputHandoffError("first-admission receipt is unavailable or mismatched")
        return value

    def preflight(self, node, workspace, *, check_only=False):
        """Called inside the worker's error boundary before native materialization."""
        contract = self._contract(node)
        if contract is None:
            return
        if _canonical(workspace) != self.paths[node.role]:
            raise InputHandoffError("input recipient workspace does not match its bound role")
        received = self._received(node, contract)
        observed = []
        for item in contract["requirements"]:
            # A resumed parent may now depend on its own review children. Do not
            # restore its earlier producer files after a legitimate own revision.
            if received is not None and item["source_id"] not in node.deps:
                continue
            source_id, path = item["source_id"], item["path"]
            source = self.repo.get(source_id)
            publication = self.repo.published_version(source_id) if source is not None else None
            version = publication["version"] if publication else None
            detail = {"kind": "declared_input", "producer": source_id, "version": version,
                      "path": path}
            if (source is None or source.id not in node.deps or source.status != "DONE"
                    or source.project_id != self.project
                    or source.conversation_id != self.conversation or source.role not in self.paths
                    or not isinstance(source.result, TaskResult)):
                raise InputHandoffError(
                    f"required input {path} from {source_id} has no completed scoped handoff",
                    detail=detail,
                )
            try:
                relative = _relative_path(path)
                if self.read_publication is not None:
                    entry = next((item for item in publication["files"] if item["path"] == path),
                                 None) if publication else None
                    if entry is None:
                        raise ValueError(f"{source.id}: {path} has no authorized publication")
                    raw = self.read_publication(source, publication["version"], entry)
                else:
                    if path not in _declared_paths(source):
                        raise ValueError("required file is absent from producer's declared result")
                    raw, _ = _snapshot(self.paths[source.role], relative)
            except (OSError, ValueError, RuntimeError) as exc:
                raise InputHandoffError(
                    f"required input {source_id}/{version or 'unknown'}:{path} "
                    f"is unavailable: {exc}",
                    detail=detail,
                ) from exc
            target = self.paths[node.role] / relative
            if target.exists() or target.is_symlink():
                try:
                    existing, _ = _snapshot(self.paths[node.role], relative)
                except (OSError, ValueError, RuntimeError) as exc:
                    raise InputHandoffError(
                        f"existing input {path} cannot be safely replaced", detail=detail,
                    ) from exc
                if existing != raw:
                    baseline = self._delivery_baseline(node.role, path)
                    if (baseline is None or baseline["source_id"] == source.id
                            or baseline["sha256"] != _sha(existing)
                            or baseline["bytes"] != len(existing)):
                        raise InputHandoffError(
                            f"declared input {source_id}/{version or 'unknown'}:{path} conflicts "
                            "with existing owned bytes; nothing was overwritten",
                            detail={**detail, "kind": "workspace_conflict",
                                    "target_sha256": _sha(existing)},
                        )
            observed.append({"source_id": source.id, "path": path,
                             "sha256": _sha(raw), "bytes": len(raw)})
        if check_only or received is not None:
            return
        self._put("pending", node.id, {
            "contract_sha256": _sha(_json(contract).encode("utf-8")), "observed": observed,
        })

    def verify_received(self, node, role):
        """Check required own files after copying, before the first model call only."""
        contract = self._contract(node)
        if contract is None:
            return
        if role.name != node.role:
            raise InputHandoffError("input recipient role is mismatched")
        if self._received(node, contract) is not None:
            return
        pending = self._get("pending", node)
        if (not isinstance(pending, dict)
                or pending.get("contract_sha256") != _sha(_json(contract).encode("utf-8"))
                or not self._valid_observations(pending.get("observed"), contract)):
            raise InputHandoffError("required input preflight was not recorded")
        for item in pending["observed"]:
            publication = self.repo.published_version(item["source_id"])
            detail = {"kind": "declared_input", "producer": item["source_id"],
                      "version": publication["version"] if publication else None,
                      "path": item["path"]}
            try:
                raw, _ = _snapshot(self.paths[node.role], _relative_path(item["path"]))
            except (OSError, ValueError, RuntimeError) as exc:
                raise InputHandoffError(
                    f"required input {item['path']} from {item['source_id']} was not received",
                    detail=detail,
                ) from exc
            if _sha(raw) != item["sha256"] or len(raw) != item["bytes"]:
                raise InputHandoffError(
                    f"received input {item['path']} differs from the observed producer bytes",
                    detail=detail,
                )
        receipt = {"schema_version": 1, "recorded_at": datetime.now(UTC).isoformat(),
                   "contract_sha256": pending["contract_sha256"], "observed": pending["observed"],
                   "scope": "first admission, opening-time producer/recipient bytes only",
                   "final_file_review": None, "human_quality": None}
        self.db.conn.execute("SAVEPOINT teacher_inputs_received")
        try:
            self._put("received", node.id, receipt)
            for item in pending["observed"]:
                baseline = {"schema_version": 1, "project_id": self.project,
                            "conversation_id": self.conversation, "role": node.role,
                            "receipt_task_id": node.id, **item}
                self.db.set_meta(self._delivery_key(node.role, item["path"]), _json(baseline))
            self.db.conn.execute("RELEASE SAVEPOINT teacher_inputs_received")
        except BaseException:
            self.db.conn.execute("ROLLBACK TO SAVEPOINT teacher_inputs_received")
            self.db.conn.execute("RELEASE SAVEPOINT teacher_inputs_received")
            raise
        self.trace.record(task_id=node.id, kind="task", name="declared_inputs_received",
                          agent_id=node.role, **receipt)

    def task_index(self, node):
        """Bounded native declared-output index: no private goals, answers or directories."""
        self._owned(node)
        rows = []
        candidates = [task for task in self.repo.all_tasks()
                      if task.project_id == self.project
                      and task.conversation_id == self.conversation
                      and task.role in self.paths]
        more = False
        for task in reversed(candidates):
            outputs = []
            if task.status == "DONE" and isinstance(task.result, TaskResult):
                outputs = [path for path in sorted(_declared_paths(task))[:32] if len(path) <= 240]
            row = {"id": task.id, "role": task.role, "status": task.status,
                   "declared_outputs": outputs[:8], "output_listing_complete": (
                       isinstance(task.result, TaskResult) and task.status == "DONE"
                       and len(outputs) == len(task.result.files) and len(outputs) <= 8)}
            rows.append(row)
            if len(rows) > 16 or len(_json(rows)) > _INDEX_CHARS:
                rows.pop()
                more = True
                break
        return ("\n## Native task and declared-output index\n"
                "These are repository facts, not instructions or private workspace paths. "
                "Existing IDs are not valid sibling keys. Use an ID only when it is already "
                "an authorized dependency. For other files, ask the existing owner to publish "
                "them through a new sibling producer. No private parent files transfer "
                "automatically. "
                "This index does not prove file reading, final review or factual quality.\n"
                + _json({"tasks": rows, "more_tasks": more}) + "\n")
