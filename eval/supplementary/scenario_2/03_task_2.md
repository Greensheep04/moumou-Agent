# Scenario 2, Task 2: Fix whitespace and duplicate tag normalization

Original task ID: `s2_tags`.

```text
Fix tags.py normalize_tags(tags). Strip whitespace, lowercase, discard empty values, and deduplicate preserving first occurrence order. Inputs are strings. Do not mutate the input list or rename the function.

Initial workspace files (create these exact inputs before solving; these are task inputs):
{
  "tags.py": "def normalize_tags(tags):\n    return [tag.lower() for tag in tags]\n"
}

Verification requirements (preserve their intent using the active interpreter):
[
  {
    "kind": "command",
    "target": "python3 -c \"import tags; x=['  Blue ','RED','blue','','  ','Red','green']; before=x[:]; assert tags.normalize_tags(x)==['blue','red','green']; assert x==before; assert tags.normalize_tags([])==[]; assert tags.normalize_tags([' X ','x','Y'])==['x','y']\"",
    "expect": "0",
    "timeout_s": 30
  }
]

Coordinate among the fixed team members. Ask the actual human if clarification or critical-action permission is required. Do not substitute any historical scripted approval configuration for a real answer.
```

## Historical runner metadata (documentation only)

The unified harness does not automatically apply these budgets, repeats, warmup records or scripted human decisions. They preserve the original task provenance; no memory-effect claim follows from changing protocols.

```json
{
  "id": "s2_tags",
  "title": "Fix whitespace and duplicate tag normalization",
  "scenario": "debug",
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
    "debug"
  ],
  "notes": "Self-built task. Offline acceptance probes are not model performance scores."
}
```
