"""Tavily search with bounded JSON output and no provider-response secret leakage."""

import json
import os

import httpx

from moumou.contracts import ToolResult
from moumou.sandbox.output import json_output


class WebSearchTool:
    name = "web_search"
    description = "Search the web and return JSON results with title, url and snippet."
    action_class = "network"
    timeout_s = 30.0
    params_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 2000},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 5, "default": 5},
        },
        "required": ["query"],
    }

    def __init__(self, *, api_key=None, transport=None):
        self.api_key = api_key
        self.transport = transport

    async def run(self, args, ctx):
        try:
            query = args.get("query")
            count = args.get("max_results", 5)
            if not isinstance(query, str) or not query.strip() or len(query) > 2000:
                raise ValueError("query must contain 1-2000 characters")
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 5:
                raise ValueError("max_results must be an integer from 1 to 5")
            key = self.api_key or ctx.env.get("TAVILY_API_KEY") or os.getenv("TAVILY_API_KEY")
            if not key:
                return ToolResult(ok=False, error="TAVILY_API_KEY is not configured")
            async with httpx.AsyncClient(transport=self.transport, timeout=20) as client:
                async with client.stream(
                    "POST",
                    "https://api.tavily.com/search",
                    headers={"Authorization": "Bearer " + key},
                    json={
                        "query": query,
                        "max_results": count,
                        "search_depth": "basic",
                        "include_answer": False,
                        "include_raw_content": False,
                    },
                ) as response:
                    if response.status_code != 200:
                        return ToolResult(ok=False, error=f"search HTTP {response.status_code}")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > 2 * 1024 * 1024:
                            return ToolResult(ok=False, error="search response exceeds 2 MiB")
                        body.extend(chunk)
            payload = json.loads(body)
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise ValueError("invalid search response")
            items = []
            truncated = len(payload["results"]) > count
            for row in payload["results"][:count]:
                if not isinstance(row, dict):
                    raise ValueError("invalid search result")
                item = {}
                for field, source, limit in (
                    ("title", "title", 256),
                    ("url", "url", 1024),
                    ("snippet", "content", 1200),
                ):
                    value = row.get(source, "")
                    if not isinstance(value, str):
                        raise ValueError("invalid search result field")
                    data = value.encode("utf-8")
                    truncated |= len(data) > limit
                    item[field] = data[:limit].decode("utf-8", "ignore")
                items.append(item)
            output, clipped = json_output(items)
            return ToolResult(ok=True, stdout=output, truncated=truncated or clipped)
        except httpx.TimeoutException:
            return ToolResult(ok=False, error="search timeout")
        except httpx.HTTPError:
            return ToolResult(ok=False, error="search network error")
        except (ValueError, TypeError, AttributeError, UnicodeError):
            return ToolResult(ok=False, error="invalid search arguments or provider response")
