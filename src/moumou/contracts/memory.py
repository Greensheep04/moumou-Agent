"""Long-term memory records.

Semantics (when to write, what counts as a lesson, when to evict) belong to the
orchestrator; storage and retrieval belong to the store layer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


class MemoryRecord(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    project_id: str
    role: str                       # memory scope = project + role
    task_id: str                    # provenance: which run produced this

    situation: str                  # what was the situation
    action: str                     # what we did
    outcome: Literal["success", "failure"]
    lesson: str                     # one actionable sentence

    score: float = 1.0              # reinforced on reuse, decays when useless
    uses: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_used_at: datetime | None = None    # decay is measured from here
    deleted_at: datetime | None = None      # soft delete, keeps the audit trail
