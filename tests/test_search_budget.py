"""No paid requests: exact reuse, shared allowances and explicit human grants."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from moumou.contracts import ToolContext, ToolResult
from moumou.eval.search_budget import SearchBudgetTool
from moumou.tools.search import WebSearchTool


class Delegate:
    def __init__(self, *, fail=False, started=None, release=None):
        self.calls = []
        self.fail, self.started, self.release = fail, started, release

    async def run(self, args, ctx):
        self.calls.append(dict(args))
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.fail:
            return ToolResult(ok=False, error="provider failure")
        return ToolResult(ok=True, stdout=json.dumps([{
            "title": args["query"], "url": "https://example.test/source", "snippet": "public text",
        }]))


def context(tmp_path, task="t1"):
    return ToolContext(agent_id="planner@" + task, task_id=task, workspace=tmp_path)


def metadata(result):
    return json.loads(result.stderr.split("search_budget=", 1)[1])


def test_standard_tool_contract_is_preserved():
    for attribute in ["name", "description", "params_schema", "action_class", "timeout_s"]:
        assert getattr(SearchBudgetTool, attribute) == getattr(WebSearchTool, attribute)


async def test_exact_cache_preserves_original_retrieval_and_provider_list(tmp_path):
    delegate = Delegate()
    tool = SearchBudgetTool(tmp_path / "cache", delegate=delegate)
    first = await tool.run({"query": "  original query  "}, context(tmp_path))
    second = await tool.run({"query": "original query", "max_results": 5},
                            context(tmp_path, "t2"))
    assert first.ok and second.ok and second.stdout == first.stdout
    assert isinstance(json.loads(second.stdout), list)
    assert delegate.calls == [{"query": "original query", "max_results": 5}]
    one, two = metadata(first), metadata(second)
    assert one["provider_requests"] == 1 and two["provider_requests"] == 0
    assert two["status"] == "cache_hit" and two["cache_hit"] is True
    assert one["original_retrieved_at"] == two["original_retrieved_at"]
    assert one["source_request_id"] == two["source_request_id"]
    stats = tool.stats()
    assert stats["used"] == stats["provider_requests"] == 1
    assert stats["cache_hits"] == 1 and stats["tool_calls"] == 2


async def test_query_case_word_spacing_and_result_count_are_not_semantically_merged(tmp_path):
    delegate = Delegate()
    tool = SearchBudgetTool(tmp_path / "cache", delegate=delegate)
    for args in [{"query": "Alpha  beta"}, {"query": "Alpha beta"},
                 {"query": "alpha beta"}, {"query": "Alpha beta", "max_results": 2}]:
        assert (await tool.run(args, context(tmp_path))).ok
    assert len(delegate.calls) == tool.stats()["provider_requests"] == 4


async def test_concurrent_team_requests_share_one_actual_attempt(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    delegate = Delegate(started=started, release=release)
    tool = SearchBudgetTool(tmp_path / "cache", delegate=delegate)
    calls = [asyncio.create_task(tool.run({"query": "same request"}, context(tmp_path, f"t{i}")))
             for i in range(10)]
    await started.wait()
    await asyncio.sleep(0.01)
    release.set()
    results = await asyncio.gather(*calls)
    assert all(result.ok for result in results)
    assert len(delegate.calls) == 1
    assert tool.stats()["provider_requests"] == tool.stats()["used"] == 1
    assert tool.stats()["deduplicated"] == 9
    assert sum(metadata(result)["provider_requests"] for result in results) == 1


async def test_two_task_phases_share_six_requests_and_need_real_grant(tmp_path):
    delegate = Delegate()
    first = SearchBudgetTool(tmp_path / "cache", max_requests=6, delegate=delegate,
                             namespace="case1")
    for index in range(3):
        assert (await first.run({"query": f"phase1-{index}"}, context(tmp_path))).ok
    second = SearchBudgetTool(tmp_path / "cache", max_requests=6, delegate=delegate,
                              namespace="case1")
    for index in range(3):
        assert (await second.run({"query": f"phase2-{index}"}, context(tmp_path, "t2"))).ok
    denied = await second.run({"query": "seventh"}, context(tmp_path, "t2"))
    assert denied.ok is False and "ask_human" in denied.error
    assert len(delegate.calls) == 6 and second.stats()["remaining"] == 0
    assert second.stats()["pending_requests"][0]["requires_human"] is True
    cached = await second.run({"query": "phase1-0"}, context(tmp_path, "t2"))
    assert cached.ok and metadata(cached)["status"] == "cache_hit"
    assert len(delegate.calls) == 6
    with pytest.raises(ValueError, match="approval_id"):
        second.grant(1)
    second.grant(1, approval_id="human-response-123")
    assert first.stats()["limit"] == 7 and first.stats()["pending_requests"] == []
    assert (await second.run({"query": "seventh"}, context(tmp_path, "t2"))).ok
    assert first.stats()["used"] == first.stats()["provider_requests"] == 7
    events = [json.loads(line)
              for line in first.events_path.read_text(encoding="utf-8").splitlines()]
    assert any(row["event"] == "budget_granted" and row["approval_id"] == "human-response-123"
               for row in events)


async def test_three_scenarios_have_independent_explicit_six_request_limits(tmp_path):
    total = 0
    for number in range(3):
        tool = SearchBudgetTool(tmp_path / "cache", max_requests=6, delegate=Delegate(),
                                namespace=f"scenario-{number}")
        for query in range(6):
            assert (await tool.run({"query": str(query)}, context(tmp_path))).ok
        assert not (await tool.run({"query": "extra"}, context(tmp_path))).ok
        total += tool.stats()["provider_requests"]
    assert total == 18


async def test_failed_requests_consume_allowance_and_are_not_success_cache_hits(tmp_path):
    delegate = Delegate(fail=True)
    tool = SearchBudgetTool(tmp_path / "cache", max_requests=2, delegate=delegate)
    for _ in range(2):
        result = await tool.run({"query": "retry"}, context(tmp_path))
        assert result.ok is False and metadata(result)["provider_requests"] == 1
        assert metadata(result)["original_retrieved_at"] is None
    denied = await tool.run({"query": "retry"}, context(tmp_path))
    assert "search_budget_exhausted" in denied.error
    assert len(delegate.calls) == tool.stats()["used"] == 2
    assert tool.stats()["cache_hits"] == 0
    assert tool.stats()["provider_failures"] == 2 and tool.stats()["provider_successes"] == 0


async def test_expired_cache_does_not_claim_fresh_data(tmp_path, monkeypatch):
    import moumou.eval.search_budget as module

    clock = [1_700_000_000.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    delegate = Delegate()
    tool = SearchBudgetTool(tmp_path / "cache", delegate=delegate, ttl_s=10)
    one = await tool.run({"query": "timely"}, context(tmp_path))
    clock[0] += 11
    two = await tool.run({"query": "timely"}, context(tmp_path))
    assert metadata(one)["original_retrieved_at"] != metadata(two)["original_retrieved_at"]
    assert metadata(two)["status"] == "provider" and len(delegate.calls) == 2


async def test_default_websearch_counts_transport_attempts_and_releases_missing_key(tmp_path,
                                                                                 monkeypatch):
    seen = []

    def response(request):
        seen.append(request)
        return httpx.Response(200, json={"results": [
            {"title": "Source", "url": "https://example.test", "content": "Evidence"},
        ]})

    backend = WebSearchTool(api_key="tvly-test-private-key",
                            transport=httpx.MockTransport(response))
    tool = SearchBudgetTool(tmp_path / "cache", delegate=backend)
    assert (await tool.run({"query": "first"}, context(tmp_path))).ok
    assert len(seen) == tool.stats()["provider_requests"] == 1
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    missing = SearchBudgetTool(tmp_path / "missing", delegate=WebSearchTool(
        transport=httpx.MockTransport(lambda _: pytest.fail("no key must not start HTTP")),
    ))
    assert not (await missing.run({"query": "second"}, context(tmp_path))).ok
    assert missing.stats()["used"] == missing.stats()["provider_requests"] == 0


async def test_http_errors_are_counted_and_audit_is_recomputable(tmp_path):
    backend = WebSearchTool(api_key="tvly-test-key", transport=httpx.MockTransport(
        lambda _: httpx.Response(503),
    ))
    tool = SearchBudgetTool(tmp_path / "cache", delegate=backend)
    result = await tool.run({"query": "unavailable"}, context(tmp_path))
    assert result.ok is False and "503" in result.error
    events = [json.loads(line)
              for line in tool.events_path.read_text(encoding="utf-8").splitlines()]
    assert sum(row["event"] == "provider_request" for row in events) == 1
    assert sum(row["event"] == "request_reserved" for row in events) == 1
    assert tool.stats()["provider_requests"] == tool.stats()["used"] == 1
    assert any(row.get("metering") == "http_transport" for row in events)


async def test_cache_and_events_redact_known_and_common_credentials(tmp_path):
    secret = "tvly-test-credential-never-persist"

    class LeakingDelegate:
        api_key = secret

        async def run(self, args, ctx):
            return ToolResult(ok=True, stdout=json.dumps([{
                "title": secret, "url": "https://example.test", "snippet": "Bearer unsafe-token",
            }]), stderr="api_key=another-private-value")

    tool = SearchBudgetTool(tmp_path / "cache", delegate=LeakingDelegate())
    result = await tool.run({"query": "private " + secret}, context(tmp_path))
    assert result.ok and "[REDACTED]" in result.stdout
    for p in tool.cache_dir.iterdir():
        content = p.read_bytes()
        assert secret.encode() not in content
        assert b"another-private-value" not in content and b"unsafe-token" not in content


async def test_cancelled_request_retains_attempt_charge_and_deduplicated_waiter_finishes(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    tool = SearchBudgetTool(tmp_path / "cache", max_requests=1,
                            delegate=Delegate(started=started, release=release))
    leader = asyncio.create_task(tool.run({"query": "pending"}, context(tmp_path)))
    await started.wait()
    follower = asyncio.create_task(tool.run({"query": "pending"}, context(tmp_path, "t2")))
    await asyncio.sleep(0)
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    following = await follower
    assert following.ok is False and metadata(following)["provider_requests"] == 0
    assert tool.stats()["used"] == tool.stats()["provider_requests"] == 1


@pytest.mark.parametrize("args", [{"query": ""}, {"query": "q", "max_results": True},
                                 {"query": "q", "max_results": 6}, None])
async def test_invalid_arguments_never_consume_requests(tmp_path, args):
    delegate = Delegate()
    tool = SearchBudgetTool(tmp_path / "cache", delegate=delegate)
    assert (await tool.run(args, context(tmp_path))).ok is False
    assert not delegate.calls and tool.stats()["used"] == tool.stats()["provider_requests"] == 0


def test_existing_budget_cannot_be_silently_replaced(tmp_path):
    SearchBudgetTool(tmp_path / "cache", max_requests=6, delegate=Delegate())
    with pytest.raises(ValueError, match="approved grant"):
        SearchBudgetTool(tmp_path / "cache", max_requests=12, delegate=Delegate())
