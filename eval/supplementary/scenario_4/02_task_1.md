# Scenario 4, Task 1: Research interpreter identity from official Python sources

Original task ID: `s4_python_research`.

```text
Use web_search to consult official Python sys documentation. Write runtime.md explaining sys.executable, sys.prefix,sys.base_prefix and virtual environments. Also write runtime.json with topic=python-interpreter,executable_attribute=sys.executable,venv_prefix_attribute=sys.prefix,base_prefix_attribute=sys.base_prefix,retrieved_on as YYYY-MM-DD,sources as nonempty objects with nonempty title and HTTPS docs.python.org/library/sys.html URL. Cite exact source URLs in runtime.md. The command name python3 alone does not identify the active environment.

Verification requirements (preserve their intent using the active interpreter):
[
  {
    "kind": "command",
    "target": "python3 -c \"import json,datetime,urllib.parse,pathlib; d=json.loads(pathlib.Path('runtime.json').read_text(encoding='utf-8')); assert d['topic']=='python-interpreter' and d['executable_attribute']=='sys.executable' and d['venv_prefix_attribute']=='sys.prefix' and d['base_prefix_attribute']=='sys.base_prefix'; datetime.date.fromisoformat(d['retrieved_on']); assert isinstance(d['sources'],list) and d['sources']; assert all(isinstance(s,dict) and isinstance(s.get('title'),str) and s['title'].strip() and urllib.parse.urlsplit(s['url']).scheme=='https' and urllib.parse.urlsplit(s['url']).hostname=='docs.python.org' and '/library/sys.html' in urllib.parse.urlsplit(s['url']).path for s in d['sources']); text=pathlib.Path('runtime.md').read_text(encoding='utf-8'); assert all(v in text for v in ['sys.executable','sys.prefix','sys.base_prefix']); assert all(s['url'] in text for s in d['sources'])\"",
    "expect": "0",
    "timeout_s": 30
  }
]

Behavior requirements:
{
  "required_tools": [
    "web_search"
  ]
}

Coordinate among the fixed team members. Ask the actual human if clarification or critical-action permission is required. Do not substitute any historical scripted approval configuration for a real answer.
```

## Historical runner metadata (documentation only)

The unified harness does not automatically apply these budgets, repeats, warmup records or scripted human decisions. They preserve the original task provenance; no memory-effect claim follows from changing protocols.

```json
{
  "id": "s4_python_research",
  "title": "Research interpreter identity from official Python sources",
  "scenario": "research",
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
    "research"
  ],
  "notes": "Self-built task. Offline acceptance probes are not model performance scores."
}
```
