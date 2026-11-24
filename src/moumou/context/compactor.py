"""压缩旧工具结果和旧回合，优先保留近期证据。

删除历史时，以 assistant 消息及对应的全部 tool 消息作为一个回合。
模型摘要默认关闭。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from moumou.contracts import ContextPolicy

from .tokens import TokenBudget, estimate_messages, estimate_text

#: Never drop the pinned prefix: role + task brief must survive compaction.
DEFAULT_PINNED = 2
#: Keep at least this many recent turns so the agent still knows what it just did.
KEEP_RECENT_TURNS = 4
#: Tool output above this many characters gets collapsed.
MAX_TOOL_CHARS = 4_000
HEAD_LINES = 8
TAIL_LINES = 6
MAX_LINE_CHARS = 400

_LINE_OF_INTEREST = re.compile(
    r"(?i)error|traceback|exit_code|artifacts|failed|timeout|^ok=|^truncated_by_executor:"
)
_STREAM_REFERENCE = ("stdout_full:", "stderr_full:")
#: A tool result that looks like a failure is evidence, not clutter: keep it whole
#: until everything else has been shrunk.
_FAILURE_HINT = re.compile(r"(?i)\berror\b|traceback|exit_code[:\s]*[1-9]|\bfailed\b")

#: Appended to every shortened result so the agent can recover the full text.
_REFERENCE_NOTE = (
    "[Older tool output shortened; follow its saved reference if present, "
    "or re-read the source if needed.]"
)
_DROP_NOTE_PREFIX = "[Earlier in this task: "


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
        if _LINE_OF_INTEREST.search(line) or line.startswith(_STREAM_REFERENCE):
            keep.add(index)

    out: list[str] = []
    previous = -1
    for index in sorted(keep):
        if index > previous + 1:
            out.append(f"...[{index - previous - 1} lines omitted]...")
        line = lines[index]
        out.append(line if line.startswith(_STREAM_REFERENCE) else line[:MAX_LINE_CHARS])
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
        return self._compact(
            messages, pinned,
            trigger=self.policy.compact_trigger_tokens,
            target=self.policy.compact_target_tokens,
        )

    def compact_to(
        self,
        messages: list[dict],
        pinned: int = DEFAULT_PINNED,
        *,
        target: int,
        trigger: int | None = None,
        preserve_recent: bool = False,
    ) -> CompactionResult:
        """Force a reduction to an explicit budget, regardless of the policy trigger.

        Used by the pre-send capacity check: the request must fit the send cap, so
        waiting for the normal trigger would be too late.
        """
        return self._compact(messages, pinned, trigger=trigger or target, target=target,
                             preserve_recent=preserve_recent)

    def _compact(
        self, messages: list[dict], pinned: int, *, trigger: int, target: int,
        preserve_recent: bool = False,
    ) -> CompactionResult:
        before = self.budget.estimate(messages)

        if before <= trigger:
            return CompactionResult(applied=False, tokens_before=before, tokens_after=before)

        # 旧回合足以释放空间时，保留近期工具结果原文。
        shrunk = self._shrink_tool_results(messages, pinned, target, older_only=True)
        dropped = 0
        if self.budget.estimate(messages) > target:
            dropped = self._drop_old_turns(
                messages, pinned, target, require_target=not preserve_recent,
            )
            if not dropped and not preserve_recent:
                shrunk += self._shrink_tool_results(messages, pinned, target)
                if self.budget.estimate(messages) > target:
                    dropped = self._drop_old_turns(messages, pinned, target)
        after = self.budget.estimate(messages)
        if after <= target:
            return CompactionResult(
                applied=bool(shrunk or dropped),
                tokens_before=before,
                tokens_after=after,
                layer=2 if dropped else 1,
                dropped_turns=dropped,
                note=(f"dropped {dropped} old turn(s)" if dropped else
                      f"shrunk {shrunk} tool result(s)"),
            )

        # Layer 3 -- last resort, disabled unless explicitly wired.
        if not preserve_recent and self.policy.allow_llm_summary and self.summarizer is not None:
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
            applied=bool(shrunk or dropped),
            tokens_before=before,
            tokens_after=after,
            layer=2,
            dropped_turns=dropped,
            note="still above target after dropping turns",
        )

    # -- internals -------------------------------------------------------
    def _shrink_tool_results(
        self, messages: list[dict], pinned: int, target: int, *, older_only: bool = False
    ) -> int:
        """Shrink long tool output, cheapest-to-lose first.

        Order matters: the oldest results are the ones the agent is least likely
        to need verbatim, and a failure is evidence rather than clutter, so it
        is only shrunk once everything else has already been compacted.
        """
        turns = [turn for turn in group_turns(messages, pinned)
                 if messages[turn[0]].get("role") == "assistant"]
        recent_from = turns[-KEEP_RECENT_TURNS:][0][0] if turns else len(messages)

        def oversized(tool_message: dict) -> bool:
            return (
                _is_tool(tool_message)
                and len(str(tool_message.get("content") or "")) > MAX_TOOL_CHARS
                and not str(tool_message.get("content") or "").endswith(_REFERENCE_NOTE)
            )

        older, recent, failures = [], [], []
        for index in range(pinned, len(messages)):
            message = messages[index]
            if not oversized(message):
                continue
            bucket = failures if _FAILURE_HINT.search(
                str(message.get("content") or "")[:2_000]
            ) else (older if index < recent_from else recent)
            bucket.append(index)

        shrunk = 0
        current = estimate_messages(messages)
        buckets = (older,) if older_only else (older, recent, failures)
        for bucket in buckets:
            for index in bucket:
                message = messages[index]
                text = str(message.get("content") or "")
                shortened = shrink_text(text) + "\n" + _REFERENCE_NOTE
                saved = estimate_text(text) - estimate_text(shortened)
                if saved <= 0:
                    continue
                message["content"] = shortened
                current -= saved
                shrunk += 1
                if int(current * self.budget.correction) <= target:
                    return shrunk
        return shrunk

    def _drop_old_turns(
        self, messages: list[dict], pinned: int, target: int, *, require_target: bool = False
    ) -> int:
        turns = [turn for turn in group_turns(messages, pinned)
                 if messages[turn[0]].get("role") == "assistant"]
        if len(turns) <= KEEP_RECENT_TURNS:
            return 0

        remaining = estimate_messages(messages)
        dropped_indices = set()
        previous_note = messages[pinned]
        if (previous_note.get("role") == "user"
                and str(previous_note.get("content") or "").startswith(_DROP_NOTE_PREFIX)):
            dropped_indices.add(pinned)
            remaining -= estimate_messages([previous_note])

        artifacts = {}
        for dropped_turns, (start, end) in enumerate(turns[:-KEEP_RECENT_TURNS], 1):
            turn = messages[start:end]
            remaining -= estimate_messages(turn)
            dropped_indices.update(range(start, end))
            artifacts.update(dict.fromkeys(_collect_artifacts(turn)))
            paths = ", ".join(artifacts) or "none"
            paths = paths if len(paths) <= 400 else paths[:397] + "..."
            note = {"role": "user", "content": (
                f"{_DROP_NOTE_PREFIX}{dropped_turns} step(s) of work were elided to save "
                f"context. Files touched earlier: {paths}]"
            )}
            estimate = remaining + estimate_messages([note])
            if int(estimate * self.budget.correction) <= target:
                break
        else:
            if require_target:
                return 0

        retained = [message for index, message in enumerate(messages[pinned:], pinned)
                    if index not in dropped_indices]
        messages[:] = [*messages[:pinned], note, *retained]
        return dropped_turns
