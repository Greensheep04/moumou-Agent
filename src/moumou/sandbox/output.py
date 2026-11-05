"""Bounded UTF-8 output helpers."""

import json

MAX_OUTPUT = 8192
MARKER = "\n[truncated]"


def clip_bytes(data: bytes) -> tuple[str, bool]:
    truncated = len(data) > MAX_OUTPUT
    text = data[:MAX_OUTPUT].decode("utf-8", errors="ignore" if truncated else "replace")
    return text + (MARKER if truncated else ""), truncated


def json_output(items) -> tuple[str, bool]:
    kept = []
    used = 2
    for item in items:
        encoded = json.dumps(item, ensure_ascii=False)
        size = len(encoded.encode("utf-8")) + (2 if kept else 0)
        if used + size > MAX_OUTPUT:
            return json.dumps(kept, ensure_ascii=False), True
        kept.append(item)
        used += size
    return json.dumps(kept, ensure_ascii=False), False
