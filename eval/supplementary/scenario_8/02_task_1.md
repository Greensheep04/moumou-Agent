# Scenario 8, Task 1: Research, delegate and approve HTTP-policy cleanup

Original task ID: `s8_composite`.

```text
Use web_search to consult official RFC 9110 status-code definitions. Delegate two independent specialists with machine acceptance and complete instructions. Child A implements status_policy.py classify_status(code): informational for 100-199,success for 200-299,redirection for 300-399,client_error for 400-499,server_error for 500-599,unknown otherwise. Child B writes policy.md explaining these categories and citing an HTTPS RFC Editor RFC 9110 URL. Integrate both artifacts. Request delete_file approval before deleting legacy_policy.txt. Preserve README.txt exactly. Allow children to run concurrently when scheduling permits.

Initial workspace files (create these exact inputs before solving; these are task inputs):
{
  "legacy_policy.txt": "obsolete policy\n",
  "README.txt": "keep project instructions\n"
}

Verification requirements (preserve their intent using the active interpreter):
[
  {
    "kind": "command",
    "target": "python3 -c \"import status_policy; assert all(status_policy.classify_status(code)==want for code,want in [(99,'unknown'),(100,'informational'),(199,'informational'),(200,'success'),(299,'success'),(300,'redirection'),(399,'redirection'),(400,'client_error'),(499,'client_error'),(500,'server_error'),(599,'server_error'),(600,'unknown')])\"",
    "expect": "0",
    "timeout_s": 30
  },
  {
    "kind": "command",
    "target": "python3 -c \"from pathlib import Path; import re,os; text=Path('policy.md').read_text(encoding='utf-8'); assert all(v in text for v in ['informational','success','redirection','client_error','server_error']); assert re.search(r'https://(?:www\\.)?rfc-editor\\.org/rfc/rfc9110(?:[.#/\\s]|$)',text); assert Path('README.txt').read_bytes()==('keep project instructions'+os.linesep).encode('utf-8'); assert not Path('legacy_policy.txt').exists()\"",
    "expect": "0",
    "timeout_s": 30
  }
]

Behavior requirements:
{
  "approvals": [
    {
      "tool": "delete_file",
      "decision": "approve"
    }
  ],
  "required_tools": [
    "web_search"
  ],
  "min_children": 2,
  "parallel_children": true
}

Coordinate among the fixed team members. Ask the actual human if clarification or critical-action permission is required. Do not substitute any historical scripted approval configuration for a real answer.
```

## Historical runner metadata (documentation only)

The unified harness does not automatically apply these budgets, repeats, warmup records or scripted human decisions. They preserve the original task provenance; no memory-effect claim follows from changing protocols.

```json
{
  "id": "s8_composite",
  "title": "Research, delegate and approve HTTP-policy cleanup",
  "scenario": "parallel",
  "budget": {
    "max_steps": 35,
    "max_wall_s": 600,
    "max_retries": 1
  },
  "human": {
    "default": "approve",
    "decisions": {
      "fs_delete": "approve"
    }
  },
  "repeats": 3,
  "tags": [
    "parallel",
    "research",
    "needs_approval",
    "composite"
  ],
  "notes": "Self-built task. Offline acceptance probes are not model performance scores."
}
```
