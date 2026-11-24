"""Token estimation and self-calibration.

We deliberately do not ship a tokenizer: DeepSeek's own ratios (≈0.6 token per
CJK character, ≈0.3 per other character) are close enough to be useful, and the
error is removed by calibrating against the ``usage`` every real call returns.
"""

from __future__ import annotations

import json
import re

# Official ratios from the DeepSeek token-usage guide.
CJK_PER_CHAR = 0.6
OTHER_PER_CHAR = 0.3
#: Safety margin: better to compact slightly early than to lose the response slot.
MARGIN = 1.1
#: Per-message framing cost (role, separators, tool_call scaffolding).
MESSAGE_OVERHEAD = 4


#: CJK punctuation, ideographs, compatibility forms and fullwidth forms.
_CJK = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")


def estimate_text(text: str) -> float:
    """Raw (uncalibrated) estimate for a single string."""
    if not text:
        return 0.0
    cjk = len(_CJK.findall(text))       # C-speed scan; matters on multi-MB transcripts
    other = len(text) - cjk
    return (cjk * CJK_PER_CHAR + other * OTHER_PER_CHAR) * MARGIN


def estimate_messages(messages: list[dict]) -> float:
    """Raw estimate for a whole OpenAI-style message list."""
    total = 0.0
    for message in messages:
        total += MESSAGE_OVERHEAD
        total += estimate_text(str(message.get("content") or ""))
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            total += estimate_text(str(function.get("name") or ""))
            total += estimate_text(str(function.get("arguments") or ""))
    return total


def estimate_tools(tools: list[dict] | None) -> float:
    """Raw estimate for the tool definitions sent alongside the messages.

    ``estimate_messages`` only walks messages, but the provider also bills the
    JSON schema of every tool, so a request can exceed a budget that the
    message-only estimate says is fine.
    """
    total = 0.0
    for tool in tools or []:
        total += estimate_text(json.dumps(tool, ensure_ascii=False))
    return total


class TokenBudget:
    """Estimator plus a correction factor learned from real usage.

    The correction is a linear EMA towards ``actual / raw``. It converges in a
    handful of calls and is clamped so one bad sample cannot wreck it.
    """

    def __init__(self, correction: float = 1.0, alpha: float = 0.2):
        self.correction = correction
        self.alpha = alpha

    def estimate(self, messages: list[dict]) -> int:
        return int(estimate_messages(messages) * self.correction)

    def estimate_text(self, text: str) -> int:
        return int(estimate_text(text) * self.correction)

    def estimate_request(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> int:
        """Estimate the whole request: messages plus separately sent tool schemas."""
        return self.estimate(messages) + int(estimate_tools(tools) * self.correction)

    def split_request(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> dict:
        """Same estimate, but itemised so a capacity error can name the source."""
        messages_tokens = self.estimate(messages)
        tool_tokens = int(estimate_tools(tools) * self.correction)
        tool_results = 0
        for message in messages:
            if message.get("role") == "tool":
                tool_results += self.estimate_text(str(message.get("content") or ""))
        return {
            "messages_tokens": messages_tokens,
            "tools_tokens": tool_tokens,
            "tool_result_tokens": tool_results,
            "total_tokens": messages_tokens + tool_tokens,
        }

    def observe(self, estimated: int, actual: int) -> float:
        """Feed back a real ``usage.prompt_tokens`` measurement."""
        if estimated <= 0 or actual <= 0:
            return self.correction

        raw = estimated / self.correction if self.correction else estimated
        target = actual / raw
        blended = self.correction + self.alpha * (target - self.correction)
        self.correction = max(0.5, min(2.0, blended))
        return self.correction
