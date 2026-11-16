# Scenario 3, Task 2: Add category filtering while preserving price behavior

Original task ID: `s3_catalog`.

```text
Extend catalog.py select(items,min_price=0,category=None). Preserve inclusive minimum-price filtering. Add case-insensitive category matching when category is given, preserving input order. Empty string is a real category, not no filter. Do not mutate input dictionaries/list.

Initial workspace files (create these exact inputs before solving; these are task inputs):
{
  "catalog.py": "def select(items, min_price=0):\n    return [item for item in items if item['price'] >= min_price]\n"
}

Verification requirements (preserve their intent using the active interpreter):
[
  {
    "kind": "command",
    "target": "python3 -c \"import catalog,copy; rows=[{'id':'a','price':10,'category':'Books'},{'id':'b','price':5,'category':'pens'},{'id':'c','price':8,'category':'books'},{'id':'d','price':0,'category':''}]; before=copy.deepcopy(rows); assert [r['id'] for r in catalog.select(rows,8)]==['a','c']; assert [r['id'] for r in catalog.select(rows,0,'BOOKS')]==['a','c']; assert catalog.select(rows,11)==[]; assert [r['id'] for r in catalog.select(rows,0,'')]==['d']; assert catalog.select([])==[]; assert rows==before\"",
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
  "id": "s3_catalog",
  "title": "Add category filtering while preserving price behavior",
  "scenario": "extend",
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
    "extend"
  ],
  "notes": "Self-built task. Offline acceptance probes are not model performance scores."
}
```
