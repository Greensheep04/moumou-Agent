"""Compaction: make room without paying for a summary.

Three layers, cheapest first:

    1. shrink tool results deterministically   (zero tokens, zero hallucination)
    2. drop the oldest whole turns             (zero tokens)
    3. ask the model to summarise              (off by default)

The hard constraint is that ``tool`` messages must stay attached to the
``assistant`` message that produced their ``tool_calls``. Dropping is therefore
done at turn granularity, never per message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from moumou.contracts import ContextPolicy

from .tokens import TokenBudget, estimate_messages

#: Never drop the pinned prefix: role + task brief must survive compaction.
DEFAULT_PINNED = 2
#: Keep at least this many recent turns so the agent still knows what it just did.
KEEP_RECENT_TURNS = 4
#: Tool output above this many characters gets collapsed.
MAX_TOOL_CHARS = 4_000
HEAD_LINES = 8
TAIL_LINES = 6
MAX_LINE_CHARS = 400

_LINE_OF_INTEREST = re.compile(r"(?i)error|traceback|exit_code|artifacts|failed|timeout")


@dataclass
class CompactionResult:
    applied: bool
    tokens_before: int
    tokens_after: int
    layer: int = 0
    dropped_turns: int = 0
    note: str = ""


def _is_tool(message: dict) -> bool:
    return message.get("role") == "tool"


def group_turns(messages: list[dict], pinned: int) -> list[tuple[int, int]]:
    """Split ``messages[pinned:]`` into turns: [assistant, *its tool results]."""
    turns: list[tuple[int, int]] = []
    index = pinned
    while index < len(messages):
        if messages[index].get("role") == "assistant":
            end = index + 1
            while end < len(messages) and _is_tool(messages[end]):
                end += 1
            turns.append((index, end))
            index = end
        else:
            turns.append((index, index + 1))
            index += 1
    return turns


def shrink_text(text: str, limit: int = MAX_TOOL_CHARS) -> str:
    """Collapse a long tool result but always keep the lines that matter."""
    if len(text) <= limit:
        return text

    lines = text.splitlines() or [text]
    keep: set[int] = set(range(min(HEAD_LINES, len(lines))))
    keep |= set(range(max(0, len(lines) - TAIL_LINES), len(lines)))
    for index, line in enumerate(lines):
        if _LINE_OF_INTEREST.search(line):
            keep.add(index)

    out: list[str] = []
    previous = -1
    for index in sorted(keep):
        if index > previous + 1:
            out.append(f"...[{index - previous - 1} lines omitted]...")
        out.append(lines[index][:MAX_LINE_CHARS])
        previous = index
    if previous < len(lines) - 1:
        out.append(f"...[{len(lines) - previous - 1} lines omitted]...")
    return "\n".join(out)


def _collect_artifacts(messages: list[dict]) -> list[str]:
    found: list[str] = []
    for message in messages:
        for line in str(message.get("content") or "").splitlines():
            if line.startswith("artifacts: "):
                for item in line[len("artifacts: ") :].split(","):
                    item = item.strip()
                    if item and item not in found:
                        found.append(item)
    return found


class Compactor:
    def __init__(
        self,
        budget: TokenBudget,
        policy: ContextPolicy,
        summarizer=None,
    ):
        self.budget = budget
        self.policy = policy
        self.summarizer = summarizer      # optional callable(list[dict]) -> str

    def compact(self, messages: list[dict], pinned: int = DEFAULT_PINNED) -> CompactionResult:
        before = self.budget.estimate(messages)
        target = self.policy.compact_target_tokens

        if before <= self.policy.compact_trigger_tokens:
            return CompactionResult(applied=False, tokens_before=before, tokens_after=before)

        # Layer 1 -- deterministic shrinkage, no model involved.
        for message in messages[pinned:]:
            if _is_tool(message):
                message["content"] = shrink_text(str(message.get("content") or ""))
        after = self.budget.estimate(messages)
        if after <= target:
            return CompactionResult(
                applied=True,
                tokens_before=before,
                tokens_after=after,
                layer=1,
                note="shrunk tool output",
            )

        # Layer 2 -- drop the oldest whole turns.
        dropped = self._drop_old_turns(messages, pinned, target)
        after = self.budget.estimate(messages)
        if after <= target:
            return CompactionResult(
                applied=True,
                tokens_before=before,
                tokens_after=after,
                layer=2,
                dropped_turns=dropped,
                note=f"dropped {dropped} old turn(s)",
            )

        # Layer 3 -- last resort, disabled unless explicitly wired.
        if self.policy.allow_llm_summary and self.summarizer is not None:
            summary = self.summarizer(messages[pinned:])
            if summary:
                messages[:] = messages[:pinned] + [
                    {"role": "user", "content": f"[Summary of earlier work]\n{summary}"}
                ]
                after = self.budget.estimate(messages)
                return CompactionResult(
                    applied=True,
                    tokens_before=before,
                    tokens_after=after,
                    layer=3,
                    dropped_turns=dropped,
                    note="llm summary",
                )

        return CompactionResult(
            applied=True,
            tokens_before=before,
            tokens_after=after,
            layer=2,
            dropped_turns=dropped,
            note="still above target after dropping turns",
        )

    # -- internals -------------------------------------------------------
    def _drop_old_turns(self, messages: list[dict], pinned: int, target: int) -> int:
        turns = group_turns(messages, pinned)
        if len(turns) <= KEEP_RECENT_TURNS:
            return 0

        # Estimate is additive over messages, so we can pick the cut point exactly
        # instead of dropping one turn and re-measuring the whole transcript.
        turn_tokens = [estimate_messages(messages[start:end]) for start, end in turns]
        correction = self.budget.correction
        head = estimate_messages(messages[:pinned])
        # Upper bound for the note we will insert; the real note is shorter.
        note_upper = estimate_messages([{"role": "user", "content": "x" * 400}])

        limit = len(turns) - KEEP_RECENT_TURNS
        best = limit
        suffix = sum(turn_tokens[limit:])
        for k in range(limit, -1, -1):
            if k < limit:
                suffix += turn_tokens[k]
            if int((head + note_upper + suffix) * correction) <= target:
                best = k
                break

        if best == 0:
            return 0

        cut = turns[best][0]
        dropped_turns = best
        dropped_messages = [m for start, end in turns[:best] for m in messages[start:end]]
        artifacts = _collect_artifacts(dropped_messages)
        note = (
            f"[Earlier in this task: {dropped_turns} step(s) of work were elided to save "
            f"context. Files touched earlier: {', '.join(artifacts) if artifacts else 'none'}]"
        )
        messages[:] = messages[:pinned] + [{"role": "user", "content": note}] + messages[cut:]
        return dropped_turns
