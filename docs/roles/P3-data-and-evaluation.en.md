# P3 — Data and Evaluation

[简体中文](P3-数据与评测层.md) · [English](P3-data-and-evaluation.en.md)

> **The only person in the group who can answer "is this system actually any good".**
> Without you, the demo is reduced to "I think it runs".

| Item | Detail |
| --- | --- |
| Course features covered | 4 the storage side of memory and evolution · both mandatory metrics (completion rate / execution time) |
| Estimated effort | about 20% (≈14 person-days) |
| Upstream dependency | P0's `contracts/` and `trace/` (delivered by the end of week 1, frozen afterwards) |
| Downstream consumers | yourself (evaluation), P2 (replay rendering), P0 (reports) |
| Key risk | the official test set is not published yet → **build your own 15-task set now**, do not wait |

---

## 1. What you own

| Directory | Technical name | Purpose |
| --- | --- | --- |
| `store/` | persistence (repository) | store the run history and agent memory, keyword searchable · **to build** |
| `eval/tasks/` | task fixtures | 15 tasks of our own · **3 samples and the format already exist** |
| `src/moumou/eval/loader.py` | task loader | YAML → `TaskFixture` · **provided** |
| `src/moumou/eval/runner.py` | evaluation harness | batch runs, isolation, scripted human · **reference implementation provided** |
| `src/moumou/eval/metrics.py` | metrics | completion rate, time breakdown, token cost · **to add** |
| `src/moumou/eval/ablation.py` | ablation study | memory on/off, parallel on/off · **to add** |
| `reports/` + CI | reporting and test infrastructure | markdown tables and charts; GitHub Actions · **to build** |

---

## 2. Interfaces to implement (frozen)

```python
# contracts/store.py — provided by P0; you write the implementations
from typing import Protocol


class TraceStore(Protocol):
    def append(self, e: TraceEvent) -> None: ...
    def query(self, task_id: str) -> list[TraceEvent]: ...      # ascending by time


class MemoryStore(Protocol):
    def write(self, rec: MemoryRecord) -> str: ...              # returns the id
    def search(self, scope: str, query: str, k: int = 5) -> list[MemoryRecord]: ...
    def all(self, scope: str) -> list[MemoryRecord]: ...        # needed by eviction
    def update_score(self, rec_id: str, delta: float, used: bool = True) -> None: ...
    def evict_candidates(self, scope: str, threshold: float = 0.3) -> list[str]: ...
    def mark_deleted(self, rec_ids: list[str]) -> None: ...
```

```python
# contracts/human.py — the second HumanIO implementation (important!)
class HumanIO(Protocol):
    async def ask(self, req: HumanRequest) -> HumanResponse: ...
    async def notify(self, msg: Message) -> None: ...


class ScriptedHuman:          # ← you implement this
    """Answers from YAML fixtures so tasks with approvals can run unattended."""
```

### Why `ScriptedHuman` is the most important small thing in the project

The course requires "tasks that need human confirmation" (scenario 5). But **evaluation has to run
unattended**, otherwise every task with an approval is excluded from batch testing and half of the
completion-rate metric becomes meaningless.

With `ScriptedHuman`:

- during a demo, P2's real CLI is plugged in and a person answers
- during a batch run, `ScriptedHuman` answers "approve" or "reject" from YAML

One interface, two implementations. **That is the real value of the abstraction, and it deserves its
own slide in the demo.**

---

## 3. Metric definitions (write them down, or the numbers will not hold)

| Metric | Definition | Why this way |
| --- | --- | --- |
| **Task completion rate** | tasks passing every `TaskNode.acceptance[]` check ÷ total tasks | "done or not" cannot be a human judgement. A criterion must be machine-checkable: a test passes, a file exists, content matches |
| **Execution time** | wall clock from `submit` to a terminal state, **split into three**: model inference / tool execution / waiting for a human | a single total has no diagnostic value; the split shows where the bottleneck is |
| **Token cost** | input + output tokens | our own addition; cost control in multi-agent systems is a real problem |

Also: **run every task at least three times** and report mean ± variance. The model is stochastic;
one run gets challenged in the demo.

---

## 4. Implementation conventions

1. `query(task_id)` returns ascending by time and must allow the span tree to be rebuilt.
2. `search` **isolates by scope** — one agent's memory must not be retrievable by another. That is
   the evidence for "each agent has its own memory".
3. The runner must be able to **kill a task that overruns**, so one infinite loop cannot take the
   whole batch down.
4. Write the metric definitions into the docs; the code and the document must agree.
5. Results land under `runs/<eval_id>/` so they are traceable, not merely printed to the terminal.

---

## 5. Acceptance

Three commands that have to work:

```bash
make eval        # 15 tasks, completion rate / timing / tokens
make report      # markdown report plus charts
make ablation    # memory on/off, parallel on/off, four groups
```

And: whatever lands under `runs/` must be readable by P2's replay.

---

## 6. Yours to decide

- SQLite table design, and whether to use FTS
- what the 15 tasks actually test (**cover the seven course scenarios, roughly two each**)
- how to draw charts (matplotlib, or plain markdown tables)
- whether to build a web dashboard

---

## 7. Out of scope

- no framework logic, no orchestration
- no memory *policy* ("when to write, what to write, when to evict" is P0's semantic layer; you
  provide storage and retrieval)
- no prompts
- no UI

---

## 8. The one thing to be able to say at the demo

> "The completion rate is not something we claim: it comes from 15 tasks of our own, machine-checkable
> acceptance criteria, and three runs per task. We also ran ablations — with memory switched off the
> rate fell from X% to Y%, which shows the memory mechanism is really doing something."

That paragraph is the most persuasive part of the whole project, **and only you can provide it**.
