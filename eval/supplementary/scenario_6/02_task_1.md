# Scenario 6, Task 1: Transfer a CSV parsing lesson to a held-out customer import

Original task ID: `s6_csv_memory`.

```text
Apply relevant historical CSV parsing experience to a new customer import. Create customers.py parse_customers(text), handling UTF-8 BOM, quoted commas,Unicode names and empty email strings. Return dictionaries with name/email strings. Read customers.csv and write customers.json. Use correct CSV parsing rather than splitting commas.

Original preparation exercise: before solving the main task, delegate and complete the following warm-up as a separate activity, verify it, and retain the reusable lesson. Do not preload an answer or claim memory transfer without observed evidence. The unified conversation protocol does not recreate the old isolated ablation experiment.
{
  "goal": "Fix inventory.py parse_inventory(text) to parse inventory CSV rows with quoted commas in label fields and return dictionaries preserving header names. Verify general parsing and extract reusable structured-delimiter lessons. This historical dataset has different columns/data from later held-out imports.",
  "setup": {
    "inventory.py": "def parse_inventory(text):\n    rows = [line.split(',') for line in text.splitlines()]\n    return [dict(zip(rows[0], row)) for row in rows[1:]]\n"
  },
  "acceptance": [
    {
      "kind": "command",
      "target": "python3 -c \"import inventory; assert inventory.parse_inventory('sku,label\\nx1,\\\"blue, large\\\"\\n')==[{'sku':'x1','label':'blue, large'}]; assert inventory.parse_inventory('sku,label\\n')==[]\"",
      "expect": "0",
      "timeout_s": 30
    }
  ],
  "budget": {
    "max_steps": 25,
    "max_wall_s": 420,
    "max_retries": 1
  }
}

Initial workspace files (create these exact inputs before solving; these are task inputs):
{
  "customers.csv": "﻿name,email\n\"Lee, Kim\",lee@example.test\nZoë,\n"
}

Verification requirements (preserve their intent using the active interpreter):
[
  {
    "kind": "command",
    "target": "python3 -c \"import customers; assert customers.parse_customers('﻿name,email\\n\\\"Lee, Kim\\\",lee@example.test\\nZoë,\\n')==[{'name':'Lee, Kim','email':'lee@example.test'},{'name':'Zoë','email':''}]; assert customers.parse_customers('name,email\\nA,a@example.test\\n')==[{'name':'A','email':'a@example.test'}]; assert customers.parse_customers('name,email\\n')==[]\"",
    "expect": "0",
    "timeout_s": 30
  },
  {
    "kind": "command",
    "target": "python3 -c \"import json,pathlib; assert json.loads(pathlib.Path('customers.json').read_text(encoding='utf-8'))==[{'name':'Lee, Kim','email':'lee@example.test'},{'name':'Zoë','email':''}]\"",
    "expect": "0",
    "timeout_s": 30
  }
]

Behavior requirements:
{
  "memory_injection": true
}

Coordinate among the fixed team members. Ask the actual human if clarification or critical-action permission is required. Do not substitute any historical scripted approval configuration for a real answer.
```

## Historical runner metadata (documentation only)

The unified harness does not automatically apply these budgets, repeats or scripted human decisions. They preserve the original task provenance; no memory-effect claim follows from changing protocols.

```json
{
  "id": "s6_csv_memory",
  "title": "Transfer a CSV parsing lesson to a held-out customer import",
  "scenario": "use_memory",
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
    "use_memory"
  ],
  "notes": "Self-built task. Offline acceptance probes are not model performance scores.",
  "warmup": {
    "goal": "Fix inventory.py parse_inventory(text) to parse inventory CSV rows with quoted commas in label fields and return dictionaries preserving header names. Verify general parsing and extract reusable structured-delimiter lessons. This historical dataset has different columns/data from later held-out imports.",
    "setup": {
      "inventory.py": "def parse_inventory(text):\n    rows = [line.split(',') for line in text.splitlines()]\n    return [dict(zip(rows[0], row)) for row in rows[1:]]\n"
    },
    "acceptance": [
      {
        "kind": "command",
        "target": "python3 -c \"import inventory; assert inventory.parse_inventory('sku,label\\nx1,\\\"blue, large\\\"\\n')==[{'sku':'x1','label':'blue, large'}]; assert inventory.parse_inventory('sku,label\\n')==[]\"",
        "expect": "0",
        "timeout_s": 30
      }
    ],
    "budget": {
      "max_steps": 25,
      "max_wall_s": 420,
      "max_retries": 1
    }
  }
}
```
