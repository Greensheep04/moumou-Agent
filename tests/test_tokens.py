"""Token estimates must follow the vendor ratios and calibrate the residual error."""

from __future__ import annotations

import pytest

from moumou.context.tokens import TokenBudget, estimate_messages, estimate_text


def test_cjk_ratio_matches_the_vendor_guidance():
    # ~0.6 token per CJK char, times our 1.1 safety margin.
    assert estimate_text("中" * 1000) == pytest.approx(1000 * 0.6 * 1.1, rel=0.01)


def test_ascii_ratio_matches_the_vendor_guidance():
    assert estimate_text("a" * 1000) == pytest.approx(1000 * 0.3 * 1.1, rel=0.01)


def test_empty_is_zero():
    assert estimate_text("") == 0.0


def test_messages_carry_per_message_overhead():
    one = estimate_messages([{"role": "user", "content": "x"}])
    two = estimate_messages([{"role": "user", "content": "x"}] * 2)
    assert two > one * 1.5, "framing cost must scale with the message count"


def test_tool_call_arguments_are_counted():
    plain = estimate_messages([{"role": "assistant", "content": ""}])
    with_call = estimate_messages(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "shell", "arguments": '{"command": "ls -la"}'},
                    }
                ],
            }
        ]
    )
    assert with_call > plain


def test_calibration_converges_on_a_consistent_bias():
    budget = TokenBudget()
    messages = [{"role": "user", "content": "a" * 2000}]
    raw = estimate_messages(messages)

    for _ in range(60):                     # the model really uses 1.5x our estimate
        budget.observe(budget.estimate(messages), int(raw * 1.5))

    assert budget.correction == pytest.approx(1.5, abs=0.05)


def test_calibration_survives_a_wild_sample():
    budget = TokenBudget()
    messages = [{"role": "user", "content": "a"}]
    for _ in range(100):
        budget.observe(budget.estimate(messages), 10**9)
    assert 0.5 <= budget.correction <= 2.0, "one bad sample must not wreck the estimate"


def test_observe_ignores_impossible_measurements():
    budget = TokenBudget()
    budget.observe(0, 100)
    budget.observe(100, 0)
    assert budget.correction == 1.0
