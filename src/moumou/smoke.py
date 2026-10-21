"""Connectivity smoke test for the real DeepSeek API.

Run it after you put a key in ``.env``:

    python -m moumou.smoke

It checks three things a unit test cannot: the key works, the model id is
accepted, and function calling comes back in the shape our orchestrator expects.
"""

from __future__ import annotations

import asyncio
import sys
import time

from moumou.adapters import DeepSeekClient
from moumou.config import LLMSettings
from moumou.tools import default_tools

ECHO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "echo",
        "description": "Echo text back",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
}


async def main() -> int:
    settings = LLMSettings.from_env()
    try:
        settings.require_key()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    client = DeepSeekClient(
        api_key=settings.api_key, base_url=settings.base_url, model=settings.model
    )
    print(f"base_url : {settings.base_url}")
    print(f"model    : {settings.model}")

    try:
        started = time.monotonic()
        plain = await client.chat(
            [{"role": "user", "content": "Reply with exactly one word: pong"}]
        )
        print(f"\n[1] plain chat  {time.monotonic() - started:.2f}s")
        print(f"    content   : {plain.content.strip()[:80]!r}")
        print(f"    tokens    : {plain.tokens_in} in / {plain.tokens_out} out")

        started = time.monotonic()
        tooling = await client.chat(
            [{"role": "user", "content": "Call the echo tool with the text 'hi'."}],
            tools=[ECHO_SCHEMA],
        )
        print(f"\n[2] tool call   {time.monotonic() - started:.2f}s")
        if not tooling.tool_calls:
            print("    WARNING: the model returned no tool call; decomposition may break")
            return 1
        call = tooling.tool_calls[0]
        print(f"    tool      : {call.name}  args={call.args}")

        advertised = {t.name for t in default_tools()}
        print(f"\n[3] orchestrator advertises {len(advertised)} tools: {sorted(advertised)}")
    finally:
        await client.aclose()

    print("\nOK — the API path is wired correctly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
