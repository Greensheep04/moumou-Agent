"""Search HTTP boundary: real normalization, bounded output and useful failures."""

import json

import httpx
import pytest

from moumou.contracts import ToolContext
from moumou.tools import default_tools


def search_tool(**kwargs):
    from moumou.tools import search

    return search.WebSearchTool(**kwargs)


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(agent_id="a", task_id="t", workspace=tmp_path)


async def test_search_is_registered_and_key_missing_is_explicit(ctx, monkeypatch):
    assert "web_search" in {t.name for t in default_tools()}
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    result = await search_tool().run({"query": "agents"}, ctx)
    assert not result.ok and "TAVILY_API_KEY" in result.error


async def test_search_normalizes_structured_results(ctx):
    def respond(request):
        assert str(request.url) == "https://api.tavily.com/search"
        assert request.headers["Authorization"] == "Bearer test-key"
        payload = json.loads(request.content)
        assert payload["query"] == "agents" and payload["max_results"] == 2
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "A",
                        "url": "https://example.org/a",
                        "content": "snippet",
                        "score": 0.9,
                    },
                    {"title": "B", "url": "https://example.org/b", "content": "next", "score": 0.8},
                ]
            },
        )

    result = await search_tool(api_key="test-key", transport=httpx.MockTransport(respond)).run(
        {"query": "agents", "max_results": 2}, ctx
    )
    assert result.ok
    assert json.loads(result.stdout) == [
        {"title": "A", "url": "https://example.org/a", "snippet": "snippet"},
        {"title": "B", "url": "https://example.org/b", "snippet": "next"},
    ]


@pytest.mark.parametrize("status", [401, 429, 500])
async def test_search_http_failure_does_not_expose_response_or_key(ctx, status):
    transport = httpx.MockTransport(lambda req: httpx.Response(status, text="test-key private"))
    result = await search_tool(api_key="test-key", transport=transport).run({"query": "q"}, ctx)
    assert not result.ok and str(status) in result.error
    assert "test-key" not in result.error


async def test_search_timeout_and_malformed_json_are_failures(ctx):
    def timeout(request):
        raise httpx.ReadTimeout("secret detail", request=request)

    result = await search_tool(api_key="k", transport=httpx.MockTransport(timeout)).run(
        {"query": "q"}, ctx
    )
    assert not result.ok and "timeout" in result.error
    malformed = httpx.MockTransport(lambda r: httpx.Response(200, text="not-json"))
    result = await search_tool(api_key="k", transport=malformed).run({"query": "q"}, ctx)
    assert not result.ok


async def test_search_large_snippets_stay_valid_json(ctx):
    transport = httpx.MockTransport(
        lambda r: httpx.Response(
            200,
            json={
                "results": [
                    {"title": "中" * 1000, "url": "https://example.org", "content": "中" * 9000}
                    for _ in range(5)
                ]
            },
        )
    )
    result = await search_tool(api_key="k", transport=transport).run({"query": "q"}, ctx)
    assert result.ok and result.truncated
    assert len(result.stdout.encode()) <= 8192
    assert json.loads(result.stdout)


async def test_search_empty_results_and_invalid_inputs(ctx):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"results": []}))
    tool = search_tool(api_key="k", transport=transport)
    result = await tool.run({"query": "q"}, ctx)
    assert result.ok and json.loads(result.stdout) == []
    for args in (
        {},
        {"query": ""},
        {"query": "q", "max_results": True},
        {"query": "q", "max_results": 100},
    ):
        assert not (await tool.run(args, ctx)).ok
