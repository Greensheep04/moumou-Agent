# Scenario 2, Task 1: Fix addition and retain the script entry point

Original task ID: `s2_bugfix`.

```text
Fix calc.py add(a,b) for positive, zero, negative and fractional values. Keep the name, argument order and __main__ entry point. Direct execution must still print 5 followed by a newline. Do not return a constant.

Initial workspace files (create these exact inputs before solving; these are task inputs):
{
  "calc.py": "def add(a, b):\n    return a - b\n\n\nif __name__ == '__main__':\n    print(add(2, 3))\n"
}

Verification requirements (preserve their intent using the active interpreter):
[
  {
    "kind": "command",
    "target": "python3 -c \"import calc; assert all(calc.add(a,b)==want for a,b,want in [(2,3,5),(0,0,0),(-4,2,-2),(7,-9,-2),(0.5,1.25,1.75),(3,2,5)])\"",
    "expect": "0",
    "timeout_s": 30
  },
  {
    "kind": "command",
    "target": "python3 -c \"import ast,pathlib,subprocess,sys; source=pathlib.Path('calc.py').read_text(encoding='utf-8'); assert any(isinstance(n,ast.If) and '__name__' in ast.unparse(n.test) and '__main__' in ast.unparse(n.test) for n in ast.parse(source).body); r=subprocess.run([sys.executable,'calc.py'],capture_output=True,text=True); assert (r.returncode,r.stdout,r.stderr)==(0,'5\\n','')\"",
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
  "id": "s2_bugfix",
  "title": "Fix addition and retain the script entry point",
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
