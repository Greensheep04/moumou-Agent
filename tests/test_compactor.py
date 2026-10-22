"""Compaction behaviour.

600k is a guardrail that real runs rarely reach, so these tests drive it with
synthetic transcripts rather than waiting for a demo to grow that large.
"""

from __future__ import annotations

import pytest

from moumou.context.compactor import (
    Compactor,
    group_turns,
    shrink_text,
)
from moumou.context.tokens import TokenBudget, estimate_messages
from moumou.contracts import ContextPolicy

PINNED = 2


def transcript(turns: int, chars: int) -> list[dict]:
    messages = [
        {"role": "system", "content": "You are a careful engineer."},
        {"role": "user", "content": "Build the thing."},
    ]
    for index in range(turns):
        messages.append(
            {
                "role": "assistant",
                "content": f"step {index}",
                "tool_calls": [
                    {
                        "id": f"c{index}",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            }
        )
        messages.append(
            {"role": "tool", "tool_call_id": f"c{index}", "content": "x" * chars}
        )
    return messages


def assert_no_orphan_tools(messages: list[dict]) -> None:
    pending: set[str] = set()
    for message in messages:
        if message["role"] == "assistant":
            pending = {c["id"] for c in message.get("tool_calls") or []}
        elif message["role"] == "tool":
            assert message["tool_call_id"] in pending, "tool result lost its assistant"


def test_group_turns_keeps_results_attached_to_their_calls():
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}, {"id": "b"}]},
        {"role": "tool", "tool_call_id": "a", "content": "x"},
        {"role": "tool", "tool_call_id": "b", "content": "y"},
    ]
    assert group_turns(messages, PINNED) == [(2, 5)]


def test_shrink_text_keeps_error_lines():
    body = "\n".join(["padding"] * 400 + ["ERROR: something exploded"] + ["padding"] * 400)
    out = shrink_text(body)

    assert "ERROR: something exploded" in out
    assert len(out) < len(body)
    assert "lines omitted" in out


def test_small_context_is_untouched():
    compactor = Compactor(TokenBudget(), ContextPolicy())
    messages = transcript(turns=3, chars=100)
    snapshot = list(messages)

    result = compactor.compact(messages, PINNED)

    assert not result.applied
    assert messages == snapshot


def test_layer_one_shrinks_huge_tool_results():
    compactor = Compactor(TokenBudget(), ContextPolicy())
    messages = transcript(turns=70, chars=30_000)
    assert estimate_messages(messages) > 600_000, "fixture must actually exceed the trigger"

    result = compactor.compact(messages, PINNED)

    assert result.applied
    assert result.layer == 1, "deterministic shrinkage alone should be enough here"
    assert result.tokens_after <= 256_000
    assert result.dropped_turns == 0


def test_layer_two_drops_old_turns_when_shrinking_is_not_enough():
    compactor = Compactor(TokenBudget(), ContextPolicy())
    messages = transcript(turns=500, chars=3_900)     # each result is below the shrink limit
    assert estimate_messages(messages) > 600_000

    result = compactor.compact(messages, PINNED)

    assert result.applied
    assert result.layer == 2
    assert result.dropped_turns > 0
    assert result.tokens_after <= 256_000


def test_pinned_prefix_survives_every_layer():
    compactor = Compactor(TokenBudget(), ContextPolicy())
    messages = transcript(turns=500, chars=3_900)
    head = [dict(m) for m in messages[:PINNED]]

    compactor.compact(messages, PINNED)

    assert messages[:PINNED] == head, "role and task brief must never be compacted away"


def test_compaction_never_orphans_a_tool_result():
    compactor = Compactor(TokenBudget(), ContextPolicy())
    messages = transcript(turns=500, chars=3_900)

    compactor.compact(messages, PINNED)

    assert_no_orphan_tools(messages)


def test_compaction_leaves_a_note_about_the_dropped_work():
    compactor = Compactor(TokenBudget(), ContextPolicy())
    messages = transcript(turns=500, chars=3_900)

    compactor.compact(messages, PINNED)

    note = messages[PINNED]
    assert note["role"] == "user"
    assert "elided" in note["content"]


def test_policy_rejects_a_trigger_that_leaves_no_room_for_output():
    with pytest.raises(ValueError, match="prompt ceiling"):
        ContextPolicy(compact_trigger_tokens=900_000)


def test_policy_rejects_a_target_above_the_trigger():
    with pytest.raises(ValueError, match="below"):
        ContextPolicy(compact_trigger_tokens=300_000, compact_target_tokens=400_000)
