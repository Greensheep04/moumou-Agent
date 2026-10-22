"""Context thresholds, kept in one place.

Every number that decides "how much goes in" lives here so it can be reviewed and
tuned without hunting through the assembler and the compactor.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class ContextPolicy(BaseModel):
    """Quotas and trigger points for one run.

    Defaults match ``deepseek-flash``: a 1M window and a 384K maximum output,
    which leaves 616K for the prompt. The trigger sits just below that ceiling,
    so compaction is a guardrail for genuinely long tasks -- not a routine path.
    """

    context_window: int = Field(default=1_000_000, ge=1_000)
    max_output_tokens: int = Field(default=384_000, ge=0)

    compact_trigger_tokens: int = Field(default=600_000, ge=1_000)
    compact_target_tokens: int = Field(default=256_000, ge=1_000)

    opening_budget_tokens: int = Field(default=8_000, ge=500)

    memory_recall_k: int = Field(default=8, ge=1)
    memory_top_k: int = Field(default=5, ge=1)
    memory_min_score: float = Field(default=0.4, ge=0.0, le=3.0)
    board_entries: int = Field(default=3, ge=0)

    allow_llm_summary: bool = False

    @property
    def prompt_ceiling(self) -> int:
        return self.context_window - self.max_output_tokens

    @model_validator(mode="after")
    def _coherent(self) -> ContextPolicy:
        if self.compact_trigger_tokens > self.prompt_ceiling:
            raise ValueError(
                f"compact_trigger_tokens ({self.compact_trigger_tokens}) exceeds the prompt "
                f"ceiling ({self.prompt_ceiling}): the response would have no room"
            )
        if self.compact_target_tokens >= self.compact_trigger_tokens:
            raise ValueError(
                "compact_target_tokens must be below compact_trigger_tokens, otherwise "
                "compaction never creates headroom"
            )
        if self.memory_top_k > self.memory_recall_k:
            raise ValueError("memory_top_k must not exceed memory_recall_k")
        return self
