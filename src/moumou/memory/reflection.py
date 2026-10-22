"""Post-task reflection: turn a finished task into at most two reusable lessons.

Not every task deserves a model call. A task that succeeded on the first try with
few steps teaches nothing; a failure, a retry, or a long grind usually does.

The model never writes prose we have to parse: it calls a ``submit_lesson`` tool
whose argument schema is a pydantic model.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from moumou.contracts import MemoryRecord, RoleSpec, TaskNode
from moumou.tools.registry import flatten_schema

PROMPT_HEADER = """You are reviewing a finished task to extract reusable lessons.

Write at most {max_lessons} lessons that would help a future run of the same role.
A lesson is only worth keeping if it is specific and actionable, for example
"when pip cannot reach the default index, retry with the mirror before giving up".
Do not restate the task. Do not write generic advice like "test your code".
If nothing generalises, submit an empty list.

## Task
{goal}

## Outcome
status: {status}   retries: {retries}   steps: {steps}

## Files produced
{artifacts}

## What happened
{detail}
"""


class LessonDraft(BaseModel):
    situation: str = Field(description="when this applies")
    action: str = Field(description="what to do")
    outcome: Literal["success", "failure"]
    lesson: str = Field(description="one actionable sentence")


class LessonBatch(BaseModel):
    lessons: list[LessonDraft] = Field(default_factory=list)


LESSON_SCHEMA = {
    "type": "function",
    "function": {
        "name": "submit_lesson",
        "description": "Submit the lessons extracted from this task.",
        "parameters": flatten_schema(LessonBatch.model_json_schema()),
    },
}


class Reflection:
    def __init__(self, llm, store, policy, trace=None, is_duplicate=None):
        self.llm = llm
        self.store = store
        self.policy = policy
        self.trace = trace
        self.is_duplicate = is_duplicate or (lambda a, b: False)

    def should_reflect(self, node: TaskNode, steps: int) -> bool:
        """Failures always teach something; quick wins usually do not."""
        if node.status == "DEAD":
            return True
        if node.retries > 0:
            return True
        return steps > self.policy.reflect_after_steps

    async def reflect(self, node: TaskNode, role: RoleSpec, steps: int = 0) -> list[MemoryRecord]:
        if self.store is None:
            return []

        try:
            response = await self.llm.chat(
                [{"role": "user", "content": self._prompt(node, steps)}],
                [LESSON_SCHEMA],
            )
        except Exception:
            return []                       # reflection must never disturb a finished run

        if not response.tool_calls:
            return []

        try:
            batch = LessonBatch.model_validate(response.tool_calls[0].args)
        except Exception:
            return []

        written: list[MemoryRecord] = []
        existing = [r.lesson for r in self.store.all(self._scope(node))]

        for draft in batch.lessons[: self.policy.max_lessons]:
            if not draft.lesson.strip():
                continue
            if any(self.is_duplicate(draft.lesson, other) for other in existing):
                continue
            record = MemoryRecord(
                project_id=node.project_id,
                role=role.name,
                task_id=node.id,
                situation=draft.situation,
                action=draft.action,
                outcome=draft.outcome,
                lesson=draft.lesson,
            )
            self.store.write(record)
            existing.append(draft.lesson)
            written.append(record)

        if self.trace is not None and written:
            self.trace.record(
                task_id=node.id,
                kind="memory",
                name="reflected",
                lessons=len(written),
            )
        return written

    # -- internals -------------------------------------------------------
    @staticmethod
    def _scope(node: TaskNode) -> str:
        return f"{node.project_id}:{node.role}"

    def _prompt(self, node: TaskNode, steps: int) -> str:
        artifacts = ", ".join(node.artifacts[:10]) or "(none)"
        detail = node.error or ""
        if not detail and node.result is not None:
            detail = node.result.as_text(600)
        detail = (detail or "(nothing recorded)")[:600]
        return PROMPT_HEADER.format(
            max_lessons=self.policy.max_lessons,
            goal=node.goal[:300],
            status=node.status,
            retries=node.retries,
            steps=steps,
            artifacts=artifacts,
            detail=detail,
        )
