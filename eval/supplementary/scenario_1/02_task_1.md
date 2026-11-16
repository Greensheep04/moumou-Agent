# Scenario 1, Task 1: Create an exact-output hello script

Original task ID: `s1_hello`.

```text
Create hello.py. Running python3 hello.py must print exactly hello, moumou followed by one newline, exit 0 and write nothing to stderr. Run it yourself before finishing.

Verification requirements (preserve their intent using the active interpreter):
[
  {
    "kind": "file_exists",
    "target": "hello.py"
  },
  {
    "kind": "command",
    "target": "python3 -c \"import subprocess,sys; r=subprocess.run([sys.executable,'hello.py'],capture_output=True,text=True); assert (r.returncode,r.stdout,r.stderr)==(0,'hello, moumou\\n','')\"",
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
  "id": "s1_hello",
  "title": "Create an exact-output hello script",
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
