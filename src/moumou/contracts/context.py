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

    #: Hard cap on a single request's *estimated* size, checked after compaction
    #: and before the request is sent. Distinct from the compaction trigger: the
    #: trigger decides when to tidy up, this decides what must never be sent.
    #: Default sits just above the default compaction target (256k) and well
    #: below the 616k prompt ceiling. Override it explicitly when a run uses a
    #: smaller compaction target - the two are validated against each other.
    max_request_tokens: int = Field(default=300_000, ge=1_000)

    #: Longest tool body admitted into the message list on first entry. Anything
    #: longer is replaced by a head/tail window plus a re-readable reference.
    max_tool_result_chars: int = Field(default=4_000, ge=500)

    opening_budget_tokens: int = Field(default=8_000, ge=500)

    memory_recall_k: int = Field(default=8, ge=1)
    memory_top_k: int = Field(default=5, ge=1)
    memory_min_score: float = Field(default=0.4, ge=0.0, le=3.0)
    board_entries: int = Field(default=3, ge=0)

    allow_llm_summary: bool = False

    # reflection: which finished tasks are worth a second model call
    reflect_after_steps: int = Field(default=8, ge=0)
    max_lessons: int = Field(default=2, ge=0, le=5)

    # evolution
    decay_factor: float = Field(default=0.9, gt=0.0, le=1.0)
    evict_threshold: float = Field(default=0.3, ge=0.0, le=3.0)

    @property
    def prompt_ceiling(self) -> int:
        return self.context_window - self.max_output_tokens

    @model_validator(mode="after")
    def _coherent(self) -> ContextPolicy:
        # A smaller endpoint window must not inherit the 1M-window defaults: when
        # the caller did not choose a trigger/target, derive a coherent pair from
        # the ceiling instead of rejecting the policy outright.
        ceiling = self.prompt_ceiling
        if ("compact_trigger_tokens" not in self.model_fields_set
                and self.compact_trigger_tokens > ceiling):
            self.compact_trigger_tokens = int(ceiling * 0.95)
        if ("compact_target_tokens" not in self.model_fields_set
                and self.compact_target_tokens >= self.compact_trigger_tokens):
            self.compact_target_tokens = int(self.compact_trigger_tokens * 0.85)
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
        # When the send cap was not given explicitly it is kept coherent with the
        # rest of the policy: never above the prompt ceiling, never below the
        # compaction target. An explicitly supplied cap is still validated strictly.
        if "max_request_tokens" not in self.model_fields_set:
            self.max_request_tokens = max(
                self.compact_target_tokens,
                min(self.max_request_tokens, self.prompt_ceiling),
            )
        if self.compact_target_tokens > self.max_request_tokens:
            raise ValueError(
                "compact_target_tokens must not exceed max_request_tokens, otherwise a "
                "compacted request can still be over the send cap"
            )
        if self.max_request_tokens > self.prompt_ceiling:
            raise ValueError(
                "max_request_tokens exceeds the prompt ceiling: the request would have "
                "no room for the response"
            )
        if self.memory_top_k > self.memory_recall_k:
            raise ValueError("memory_top_k must not exceed memory_recall_k")
        return self
