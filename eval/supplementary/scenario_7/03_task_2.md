# Scenario 7, Task 2: Delegate independent word counts then merge

Original task ID: `s7_parallel_words`.

```text
Delegate two independent specialist subtasks with machine acceptance and no mutual dependencies,including full input in each child goal. Child A counts lowercase whitespace-separated words in red blue red and writes words_a.json. Child B counts blue green and writes words_b.json. Return JSON artifacts. Integrate to words.json containing red=2,blue=2,green=1,retaining partial files. Allow concurrency when scheduling permits.

Verification requirements (preserve their intent using the active interpreter):
[
  {
    "kind": "command",
    "target": "python3 -c \"import json,pathlib; read=lambda p:json.loads(pathlib.Path(p).read_text(encoding='utf-8')); assert read('words_a.json')=={'red':2,'blue':1}; assert read('words_b.json')=={'blue':1,'green':1}; assert read('words.json')=={'red':2,'blue':2,'green':1}\"",
    "expect": "0",
    "timeout_s": 30
  }
]

Behavior requirements:
{
  "min_children": 2,
  "parallel_children": true
}

Coordinate among the fixed team members. Ask the actual human if clarification or critical-action permission is required. Do not substitute any historical scripted approval configuration for a real answer.
```

## Historical runner metadata (documentation only)

The unified harness does not automatically apply these budgets, repeats, warmup records or scripted human decisions. They preserve the original task provenance; no memory-effect claim follows from changing protocols.

```json
{
  "id": "s7_parallel_words",
  "title": "Delegate independent word counts then merge",
  "scenario": "parallel",
  "budget": {
    "max_steps": 35,
    "max_wall_s": 600,
    "max_retries": 1
  },
  "human": {
    "default": "approve"
  },
  "repeats": 3,
  "tags": [
    "parallel"
  ],
  "notes": "Self-built task. Offline acceptance probes are not model performance scores."
}
```
