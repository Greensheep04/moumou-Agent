"""Frozen contracts. Import everything data-shaped from here, never from siblings."""

from .agent import MAX_ROLES_PER_PROJECT, AgentInstance, RoleSpec
from .human import HumanIO, HumanRequest, HumanResponse
from .memory import MemoryRecord
from .message import HUMAN_ID, Channel, Message, MessageKind, Scope
from .plan import Plan, RoleDraft, SubtaskSpec
from .store import MemoryStore, TaskRepo, TraceStore
from .task import (
    LEGAL_TRANSITIONS,
    MAX_DEPTH,
    MAX_SUBTASKS,
    TERMINAL_STATUSES,
    Acceptance,
    AcceptanceKind,
    Budget,
    TaskNode,
    TaskStatus,
    TaskSummary,
    can_transition,
)
from .tool import DEFAULT_APPROVAL, ActionClass, Tool, ToolContext, ToolResult
from .trace import SpanKind, TraceEvent
from .workspace import IntegrateResult, WorkspaceProvider

__all__ = [
    "DEFAULT_APPROVAL",
    "HUMAN_ID",
    "LEGAL_TRANSITIONS",
    "MAX_DEPTH",
    "MAX_ROLES_PER_PROJECT",
    "MAX_SUBTASKS",
    "TERMINAL_STATUSES",
    "Acceptance",
    "AcceptanceKind",
    "ActionClass",
    "AgentInstance",
    "Budget",
    "Channel",
    "HumanIO",
    "HumanRequest",
    "HumanResponse",
    "IntegrateResult",
    "MemoryRecord",
    "MemoryStore",
    "Message",
    "MessageKind",
    "Plan",
    "RoleDraft",
    "RoleSpec",
    "Scope",
    "SpanKind",
    "SubtaskSpec",
    "TaskNode",
    "TaskRepo",
    "TaskStatus",
    "TaskSummary",
    "Tool",
    "ToolContext",
    "ToolResult",
    "TraceEvent",
    "TraceStore",
    "WorkspaceProvider",
    "can_transition",
]
