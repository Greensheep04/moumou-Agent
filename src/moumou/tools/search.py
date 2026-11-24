"""Web search with bounded JSON output and sanitized provider errors."""

import json
import os

import httpx

from moumou.contracts import ToolResult
from moumou.sandbox.output import json_output

_KEY_ENV = {"tavily": "TAVILY_API_KEY", "serpapi": "SERPAPI_API_KEY"}


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

    def __init__(self, *, api_key=None, transport=None, provider=None):
        if provider is None:
            provider = os.getenv("MOUMOU_SEARCH_PROVIDER", "tavily")
        self.provider = provider
        if self.provider not in _KEY_ENV:
            raise ValueError("MOUMOU_SEARCH_PROVIDER must be tavily or serpapi")
        self.key_env = _KEY_ENV[self.provider]
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
            key = self.api_key or ctx.env.get(self.key_env) or os.getenv(self.key_env)
            if not key:
                return ToolResult(ok=False, error=f"{self.key_env} is not configured")
            if self.provider == "serpapi":
                method, endpoint = "GET", "https://serpapi.com/search.json"
                options = {"params": {"engine": "google", "q": query, "api_key": key}}
                result_field, url_field, snippet_field = "organic_results", "link", "snippet"
            else:
                method, endpoint = "POST", "https://api.tavily.com/search"
                options = {
                    "headers": {"Authorization": "Bearer " + key},
                    "json": {
                        "query": query,
                        "max_results": count,
                        "search_depth": "basic",
                        "include_answer": False,
                        "include_raw_content": False,
                    },
                }
                result_field, url_field, snippet_field = "results", "url", "content"
            async with httpx.AsyncClient(transport=self.transport, timeout=20) as client:
                async with client.stream(method, endpoint, **options) as response:
                    if response.status_code != 200:
                        return ToolResult(ok=False, error=f"search HTTP {response.status_code}")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > 2 * 1024 * 1024:
                            return ToolResult(ok=False, error="search response exceeds 2 MiB")
                        body.extend(chunk)
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("invalid search response")
            if self.provider == "serpapi":
                metadata = payload.get("search_metadata")
                if payload.get("error") or (
                    not isinstance(metadata, dict) or metadata.get("status") != "Success"
                ):
                    return ToolResult(ok=False, error="SerpApi did not complete the search")
                rows = payload.get(result_field, [])
            else:
                rows = payload.get(result_field)
            if not isinstance(rows, list):
                raise ValueError("invalid search results")
            items = []
            truncated = len(rows) > count
            for row in rows[:count]:
                if not isinstance(row, dict):
                    raise ValueError("invalid search result")
                item = {}
                for field, source, limit in (
                    ("title", "title", 256),
                    ("url", url_field, 1024),
                    ("snippet", snippet_field, 1200),
                ):
                    value = row.get(source, "")
                    if not isinstance(value, str):
                        raise ValueError("invalid search result field")
                    if self.provider == "serpapi":
                        value = value.replace(key, "[REDACTED]")
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
