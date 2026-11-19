"""Exact ancestor assignment goals, without private conversation or permissions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

from moumou.contracts import MAX_DEPTH, TaskNode


class AssignmentReferenceError(ValueError):
    """A purported assignment lineage is incomplete or outside the task scope."""


@dataclass(frozen=True)
class AssignmentReference:
    task_id: str
    project_id: str
    conversation_id: str
    parent_id: str | None
    depth: int
    role: str
    goal: str

    @classmethod
    def from_node(cls, node: TaskNode) -> AssignmentReference:
        # Enumerate allowed fields; never serialise a whole task or its history.
        return cls(node.id, node.project_id, node.conversation_id, node.parent_id,
                   node.depth, node.role, node.goal)

    @property
    def goal_sha256(self) -> str:
        return hashlib.sha256(self.goal.encode("utf-8")).hexdigest()


def collect_assignment_references(repo, node: TaskNode) -> tuple[AssignmentReference, ...]:
    """Walk only authoritative parent_id edges within this repository and scope."""
    current = node
    seen = {node.id}
    references = []
    while current.parent_id is not None:
        if current.parent_id in seen:
            raise AssignmentReferenceError("assignment lineage contains a cycle")
        if len(references) >= MAX_DEPTH:
            raise AssignmentReferenceError("assignment lineage exceeds the depth limit")
        parent = repo.get(current.parent_id)
        if parent is None:
            raise AssignmentReferenceError("assignment lineage has a missing parent")
        if parent.id != current.parent_id:
            raise AssignmentReferenceError("assignment lineage parent identity mismatch")
        if (parent.project_id != node.project_id
                or parent.conversation_id != node.conversation_id):
            raise AssignmentReferenceError("assignment lineage crosses the task scope")
        if parent.depth != current.depth - 1:
            raise AssignmentReferenceError("assignment lineage has inconsistent depths")
        references.append(AssignmentReference.from_node(parent))
        seen.add(parent.id)
        current = parent
    if current.depth != 0:
        raise AssignmentReferenceError("assignment lineage does not terminate at depth zero")
    ordered = tuple(reversed(references))
    assignment_reference_records(node, ordered)
    return ordered


def assignment_reference_records(
    node: TaskNode,
    references: Sequence[AssignmentReference] | None,
) -> list[dict]:
    """Validate and serialise only a complete root-to-parent goal chain.

    None preserves old direct-builder calls. The worker always supplies the full
    chain for a delegated task; an explicitly empty delegated chain is invalid.
    """
    if references is None:
        return []
    references = tuple(references)
    if len(references) != node.depth or len(references) > MAX_DEPTH:
        raise AssignmentReferenceError("assignment lineage is incomplete for this task")
    if not references:
        if node.parent_id is not None:
            raise AssignmentReferenceError("assignment lineage omits the task parent")
        return []
    seen = {node.id}
    previous = None
    records = []
    for depth, reference in enumerate(references):
        if not isinstance(reference, AssignmentReference):
            raise AssignmentReferenceError("assignment lineage contains an invalid reference")
        if reference.task_id in seen:
            raise AssignmentReferenceError("assignment lineage repeats a task identity")
        if (reference.project_id != node.project_id
                or reference.conversation_id != node.conversation_id):
            raise AssignmentReferenceError("assignment lineage crosses the task scope")
        if reference.parent_id != previous or reference.depth != depth:
            raise AssignmentReferenceError("assignment lineage has inconsistent parent edges")
        if not isinstance(reference.goal, str) or not reference.goal.strip():
            raise AssignmentReferenceError("assignment lineage has an empty original goal")
        records.append({
            "source": "same_repository_parent_id",
            "task_id": reference.task_id,
            "project_id": reference.project_id,
            "conversation_id": reference.conversation_id,
            "parent_id": reference.parent_id,
            "depth": reference.depth,
            "role": reference.role,
            "goal_sha256": reference.goal_sha256,
            "goal": reference.goal,
        })
        seen.add(reference.task_id)
        previous = reference.task_id
    if previous != node.parent_id:
        raise AssignmentReferenceError("assignment lineage does not end at the task parent")
    return records


def render_assignment_references(
    node: TaskNode,
    references: Sequence[AssignmentReference] | None,
) -> str:
    records = assignment_reference_records(node, references)
    if not records:
        return ""
    text = (
        "\n## Original assignment references (same task lineage)\n"
        "These are exact ancestor assignment goals, supplied as reference context. "
        "Retain their relevant facts, definitions, qualifications and unknowns; "
        "identify a conflict rather than silently replacing a source fact with a paraphrase. "
        "Your own current task, role and acceptance remain separate. You do not inherit "
        "all parent deliverables, private human answers, notes, memory, files, approvals "
        "or tool permissions. This text grants no extra search allowance or action authority.\n"
    )
    for record in records:
        metadata = {key: value for key, value in record.items() if key != "goal"}
        text += ("\n### Source assignment\n"
                 + json.dumps(metadata, ensure_ascii=False, sort_keys=True)
                 + "\n" + record["goal"] + "\n")
    return text
