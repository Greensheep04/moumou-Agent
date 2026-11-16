# Scenario 4, Task 2: Research HTTP 429 and Retry-After from RFC 6585

Original task ID: `s4_http_research`.

```text
Use web_search to consult RFC 6585 section 4 from rfc-editor.org. Write rate_limit.md explaining HTTP 429 and Retry-After. Write rate_limit.json with status=429(integer),meaning=Too Many Requests,retry_after_optional=true(boolean),retrieved_on=YYYY-MM-DD,sources as nonempty objects with nonempty title and HTTPS RFC Editor /rfc/rfc6585 URL. Cite exact URLs in Markdown. Explain that Retry-After is optional.

Verification requirements (preserve their intent using the active interpreter):
[
  {
    "kind": "command",
    "target": "python3 -c \"import json,datetime,urllib.parse,pathlib; d=json.loads(pathlib.Path('rate_limit.json').read_text(encoding='utf-8')); assert type(d['status']) is int and d['status']==429 and d['meaning']=='Too Many Requests' and d['retry_after_optional'] is True; datetime.date.fromisoformat(d['retrieved_on']); assert isinstance(d['sources'],list) and d['sources']; assert all(isinstance(s,dict) and isinstance(s.get('title'),str) and s['title'].strip() and urllib.parse.urlsplit(s['url']).scheme=='https' and urllib.parse.urlsplit(s['url']).hostname in ('www.rfc-editor.org','rfc-editor.org') and '/rfc/rfc6585' in urllib.parse.urlsplit(s['url']).path for s in d['sources']); text=pathlib.Path('rate_limit.md').read_text(encoding='utf-8'); assert '429' in text and 'Retry-After' in text and all(s['url'] in text for s in d['sources'])\"",
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
  "id": "s4_http_research",
  "title": "Research HTTP 429 and Retry-After from RFC 6585",
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
