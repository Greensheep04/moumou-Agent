# Scenario 7, Task 1: Delegate independent statistics then integrate

Original task ID: `s7_parallel_stats`.

```text
Delegate two independent specialist subtasks with machine acceptance and no mutual dependencies. Include full data in each child goal because workspaces are private. Child A values=[-1,3,5],write left.json with count=3,sum=7. Child B values=[-2,4,6],write right.json with count=3,sum=8. Children return JSON artifacts. Integrate and write combined.json with count=6,sum=15. Do not replace delegation with one agent creating every output. Allow concurrency when scheduling permits.

Verification requirements (preserve their intent using the active interpreter):
[
  {
    "kind": "command",
    "target": "python3 -c \"import json,pathlib; read=lambda p:json.loads(pathlib.Path(p).read_text(encoding='utf-8')); assert read('left.json')=={'count':3,'sum':7}; assert read('right.json')=={'count':3,'sum':8}; assert read('combined.json')=={'count':6,'sum':15}\"",
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
  "id": "s7_parallel_stats",
  "title": "Delegate independent statistics then integrate",
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
