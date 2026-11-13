"""Explicit evaluation controls and portable long-term-memory snapshots.

These controls belong to the harness. Omitting them retains the runner's
historical Engine defaults; passing them makes the experimental settings
reviewable and reproducible across in-process and supervised execution.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import PureWindowsPath
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

from moumou.contracts import MemoryRecord


def memory_snapshot(records: list[MemoryRecord]) -> list[dict]:
    """Return an ordered, independent JSON snapshot, including provenance."""
    return [
        record.model_dump(mode="json")
        for record in sorted(records, key=lambda record: record.id)
    ]


def memory_snapshot_sha256(records: list[MemoryRecord]) -> str:
    encoded = json.dumps(
        memory_snapshot(records), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class EvaluationOptions(BaseModel):
    """The deliberately small settings surface used by the ablation harness."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_enabled: StrictBool = True
    max_slots: Annotated[StrictInt, Field(ge=1, le=64)] = 4
    role_review_enabled: StrictBool = False
    project_id: str | None = None
    conversation_id: str = "heldout"
    memory_records: list[MemoryRecord] = Field(default_factory=list, max_length=1000)
    warmup_id: str | None = None

    @field_validator("project_id", "conversation_id", "warmup_id")
    @classmethod
    def _nonempty_identity(cls, value):
        if value is not None and (not value.strip() or len(value) > 200):
            raise ValueError("evaluation identities must be nonempty and at most 200 characters")
        if value is not None:
            windows = PureWindowsPath(value)
            if (value in {".", ".."} or "/" in value or "\\" in value or ":" in value
                    or windows.drive or windows.root or windows.is_reserved()
                    or value.endswith((".", " "))):
                raise ValueError("evaluation identities must be one safe directory name")
        return value

    @field_validator("memory_records")
    @classmethod
    def _valid_records(cls, records):
        if len({record.id for record in records}) != len(records):
            raise ValueError("memory snapshot contains duplicate record ids")
        if any(not math.isfinite(record.score) for record in records):
            raise ValueError("memory snapshot scores must be finite")
        # Do not retain references to mutable caller-owned MemoryRecord objects.
        return [record.model_copy(deep=True) for record in records]

    def engine_overrides(self) -> dict:
        overrides = {
            "memory_enabled": self.memory_enabled,
            "max_slots": self.max_slots,
            "role_review_enabled": self.role_review_enabled,
            "conversation_id": self.conversation_id,
        }
        if self.project_id is not None:
            overrides["project_id"] = self.project_id
        return overrides

    def memory_snapshot(self) -> list[dict]:
        return memory_snapshot(self.memory_records)

    @property
    def memory_sha256(self) -> str:
        return memory_snapshot_sha256(self.memory_records)

    def records_for_project(self, project_id: str) -> list[MemoryRecord]:
        """Reject accidental cross-project warmup; return fresh mutable copies."""
        if any(record.project_id != project_id for record in self.memory_records):
            raise ValueError("memory snapshot project does not match the held-out task")
        return [record.model_copy(deep=True) for record in self.memory_records]
