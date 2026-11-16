# Scenario 1, Task 2: Create a reusable statistics function and CLI

Original task ID: `s1_stats`.

```text
Create stats.py with describe(values), returning count, sum, min, max and mean. Empty input returns count=0,sum=0 and None for min/max/mean. Do not mutate input. Its CLI reads whitespace-separated integers from argv[1] and writes JSON to argv[2]. Use numbers.txt to produce stats.json. Verify reusable function and CLI.

Initial workspace files (create these exact inputs before solving; these are task inputs):
{
  "numbers.txt": "-3 2 10 0 8\n"
}

Verification requirements (preserve their intent using the active interpreter):
[
  {
    "kind": "command",
    "target": "python3 -c \"import stats; x=[-3,2,10,0,8]; before=x[:]; assert stats.describe(x)=={'count':5,'sum':17,'min':-3,'max':10,'mean':3.4}; assert x==before; assert stats.describe([])=={'count':0,'sum':0,'min':None,'max':None,'mean':None}; assert stats.describe([4,4])=={'count':2,'sum':8,'min':4,'max':4,'mean':4.0}\"",
    "expect": "0",
    "timeout_s": 30
  },
  {
    "kind": "command",
    "target": "python3 -c \"import json,subprocess,sys; r=subprocess.run([sys.executable,'stats.py','numbers.txt','stats.json'],capture_output=True,text=True); assert r.returncode==0; assert json.load(open('stats.json'))=={'count':5,'sum':17,'min':-3,'max':10,'mean':3.4}\"",
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
  "id": "s1_stats",
  "title": "Create a reusable statistics function and CLI",
  "scenario": "from_scratch",
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
    "from_scratch"
  ],
  "notes": "Self-built task. Offline acceptance probes are not model performance scores."
}
```
