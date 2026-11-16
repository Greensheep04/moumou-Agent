# What to do now

[简体中文](TASKS.md) · [English](TASKS.en.md)

> One page for the three of you. The detail lives in your own brief; this only says
> **where to start**.

> The Chinese [TASKS.md](TASKS.md) is the authoritative version. This file is a translation.

## 1. Get it running (10 minutes)

```bash
git clone https://github.com/Greensheep04/moumou-Agent.git
cd moumou-Agent
make install
cp .env.example .env      # put your own DeepSeek key in it
make test                 # 142 tests, no key needed
```

## 2. What you own

| You | Your first PR should be | Acceptance |
| --- | --- | --- |
| **P1 execution** | extract the path checks into a standalone `LocalJail`, then add `git`, `git_push` and `web_search` | `pytest tests/test_tool_contract.py -q` |
| **P2 interaction** | a persistent `Channel` (SQLite or JSONL), pointed at by `make_bus` in the contract test | `pytest tests/test_channel_contract.py -q` |
| **P3 data** | import `trace.jsonl` into SQLite so `make eval` runs | `make eval` |

Signatures, fields and conventions are in [the interface spec](../INTERFACES.en.md). Nothing
needs guessing.

## 3. What changed recently

The sub-agent area moved a lot lately, so older notes may not match:

| Change | Does it affect you? |
| --- | --- |
| The lead agent has its own prompt | no |
| Sub-agents report through `submit_result` (structured fields) | no |
| A dead child wakes its parent; re-dispatched children carry a round prefix | no |
| Roles can be revised, and carry a version | yes, if you read the `roles` table: `version` / `updated_at` |
| **`TaskNode.result` is a structure now, not a string** | **yes, if you read task results: take the fields** |

Details are in [the sub-agent document](../SUBAGENTS.en.md).

## 4. Running the tests

| Command | Time | What it is |
| --- | --- | --- |
| `make test` | ~2s | all 142 |
| `pytest tests/test_tool_contract.py -q` | ~1s | P1's acceptance |
| `pytest tests/test_channel_contract.py -q` | <1s | P2's acceptance |
| `make eval` | seconds per task | needs a key; P3's acceptance |

**The contract suites must stay green after you swap in your implementation.** If you need to
change anything under `src/moumou/contracts/`, raise it in the group first.

## 5. Branching

One long-lived branch each: `feat/p1-sandbox`, `feat/p2-cli`, `feat/p3-eval`.
Rebase onto main before merging so the history stays linear.

## 6. If you get stuck

Start with [the FAQ](../FAQ.en.md). If that does not answer it, post your branch and the steps to
reproduce — do not sit on it. The contract layer is shared, and a workaround invented in private is
a debt someone has to repay later.
