"""Real provenance and bounded registered-source reads; no external network required."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from moumou.contracts import ToolContext, ToolResult
from moumou.eval.search_budget import SearchBudgetTool
from moumou.eval.source_evidence import SourceEvidenceStore
from moumou.tools.source import ReadSourceTool, SourceHistoryTool


def context(tmp_path, role="research", task="t1"):
    return ToolContext(agent_id=role, task_id=task, workspace=tmp_path)


def register(store, ctx, url="https://example.test/article", **kwargs):
    return store.record_search("actual query", ToolResult(ok=True, stdout=json.dumps([
        {"url": url, "title": "A source", "snippet": "Only a search excerpt"},
    ])), ctx, retrieved_at="2026-01-03T10:00:00+00:00", request_id="old-real-request", **kwargs)


def resolver(host, port):
    return ["93.184.216.34"]


async def test_shared_history_preserves_imported_time_request_and_snippet_boundary(tmp_path):
    store = SourceEvidenceStore(tmp_path / "evidence")
    register(store, context(tmp_path), origin="imported")
    result = await SourceHistoryTool(SourceEvidenceStore(store.root)).run(
        {}, context(tmp_path, "fund_manager", "t2"),
    )
    event = json.loads(result.stdout)["events"][0]
    assert event["origin"] == "imported" and event["request_id"] == "old-real-request"
    assert event["retrieved_at"] == "2026-01-03T10:00:00+00:00"
    assert event["sources"][0]["snippet_sha256"]
    assert event["sources"][0]["verification"] == "search_snippet_only"
    assert event["agent_id"] == "research" and event["task_id"] == "t1"


async def test_search_budget_records_provider_cache_and_failure_without_extra_requests(tmp_path):
    class Delegate:
        calls = 0

        async def run(self, args, ctx):
            self.calls += 1
            return ToolResult(ok=True, stdout=json.dumps([
                {"url": "https://example.test/a", "title": "Real", "snippet": "Excerpt"},
            ]))

    store, delegate = SourceEvidenceStore(tmp_path / "evidence"), Delegate()
    search = SearchBudgetTool(tmp_path / "budget", max_requests=1,
                              delegate=delegate, source_store=store)
    await search.run({"query": "same"}, context(tmp_path))
    await search.run({"query": "same"}, context(tmp_path, "risk", "t2"))
    denied = await search.run({"query": "different"}, context(tmp_path))
    events = store.history()["events"]
    assert not denied.ok and delegate.calls == search.stats()["provider_requests"] == 1
    original = next(event for event in events if event["origin"] == "provider")
    cached = next(event for event in events if event["origin"] == "cache_hit")
    assert original["retrieved_at"] == cached["retrieved_at"]
    assert original["request_id"] == cached["request_id"]
    assert any(event["ok"] is False and event["origin"] == "budget_exhausted" for event in events)


async def test_page_is_really_read_pinned_cached_and_paginated(tmp_path):
    store, ctx, calls = SourceEvidenceStore(tmp_path / "evidence"), context(tmp_path), []
    register(store, ctx)

    def handle(request):
        calls.append(request)
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=(
            b"<html><title>Original title</title><script>BAD EXECUTION</script>"
            b"<p>The original page says something different from the snippet.</p></html>"
        ))

    tool = ReadSourceTool(store, transport=httpx.MockTransport(handle), resolver=resolver)
    one = await tool.run({"url": "https://example.test/article", "limit": 14}, ctx)
    first = json.loads(one.stdout)
    two = await tool.run({"url": first["url"], "offset": first["next_offset"]},
                         context(tmp_path, "risk", "t2"))
    second = json.loads(two.stdout)
    assert one.ok and two.ok and len(calls) == 1
    assert calls[0].url.host == "93.184.216.34" and calls[0].headers["Host"] == "example.test"
    assert calls[0].extensions["sni_hostname"] == "example.test"
    assert not first["cached"] and second["cached"]
    assert first["retrieved_at"] == second["retrieved_at"]
    assert first["raw_sha256"] and first["text_sha256"]
    combined = first["text"] + second["text"]
    assert "different from the snippet" in combined and "BAD EXECUTION" not in combined
    assert store.history()["events"][0]["agent_id"] == "risk"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/private", "http://[::1]/", "http://169.254.169.254/metadata",
    "http://localhost/a", "http://server.local/a", "https://user:password@example.com/a",
    "https://example.com/a?api_key=secret", "https://example.com/a?token=secret",
    "https://www.google.co.uk/search?q=foo", "https://search.yahoo.com/search?p=foo",
    "https://example.com/search?q=foo", "https://example.com:8080/a", "file:///etc/passwd",
])
async def test_forbidden_urls_are_not_registered_or_fetched(tmp_path, url):
    store, ctx, calls = SourceEvidenceStore(tmp_path / "evidence"), context(tmp_path), []
    register(store, ctx, url)
    tool = ReadSourceTool(store, resolver=resolver, transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200, text="no"),
    ))
    assert not (await tool.run({"url": url}, ctx)).ok and calls == []
    assert store.history()["events"][0]["kind"] == "read"


async def test_unregistered_page_and_private_or_mixed_dns_never_connect(tmp_path):
    store, ctx, calls = SourceEvidenceStore(tmp_path / "evidence"), context(tmp_path), []
    register(store, ctx)
    for addresses in (["127.0.0.1"], ["93.184.216.34", "10.0.0.1"]):
        tool = ReadSourceTool(store, resolver=lambda *args, value=addresses: value,
                              transport=httpx.MockTransport(lambda request: calls.append(request)))
        assert not (await tool.run({"url": "https://example.test/article"}, ctx)).ok
    tool = ReadSourceTool(store, resolver=resolver)
    assert not (await tool.run({"url": "https://example.test/unknown"}, ctx)).ok
    assert not calls


async def test_redirect_is_revalidated_and_records_final_url(tmp_path):
    store, ctx, calls = SourceEvidenceStore(tmp_path / "evidence"), context(tmp_path), []
    register(store, ctx)

    def handle(request):
        calls.append(request)
        if request.url.path == "/article":
            return httpx.Response(302, headers={"Location": "https://publisher.test/final"})
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, text="Real final text")

    tool = ReadSourceTool(store, transport=httpx.MockTransport(handle), resolver=resolver)
    result = await tool.run({"url": "https://example.test/article"}, ctx)
    assert result.ok and json.loads(result.stdout)["final_url"] == "https://publisher.test/final"
    assert len(calls) == 2 and calls[-1].headers["Host"] == "publisher.test"
    blocked = ReadSourceTool(store, cache_ttl_s=0, resolver=resolver,
                             transport=httpx.MockTransport(lambda request: httpx.Response(
                                 302, headers={"Location": "http://127.0.0.1/secret"},
                             )))
    assert not (await blocked.run({"url": "https://example.test/article"}, ctx)).ok


@pytest.mark.parametrize(("response", "error"), [
    (httpx.Response(403, text="Forbidden"), "HTTP 403"),
    (httpx.Response(200, headers={"Content-Type": "application/pdf"}, content=b"%PDF"), "format"),
    (httpx.Response(200, headers={"Content-Type": "text/plain"}, content=b"%PDF binary"), "PDF"),
    (httpx.Response(200, headers={"Content-Type": "text/plain"}, text="x" * 101), "size"),
    (httpx.Response(200, headers={"Content-Type": "text/html"}, text="Verify you are human"),
     "barrier"),
])
async def test_failures_remain_evidence_not_verified_pages(tmp_path, response, error):
    store, ctx = SourceEvidenceStore(tmp_path / "evidence"), context(tmp_path)
    register(store, ctx)
    tool = ReadSourceTool(store, max_bytes=100, resolver=resolver,
                          transport=httpx.MockTransport(lambda request: response))
    result = await tool.run({"url": "https://example.test/article"}, ctx)
    assert not result.ok and error in result.error
    assert store.document("https://example.test/article") is None
    assert store.history()["events"][0]["ok"] is False


async def test_bounded_timeout_and_redirect_loop(tmp_path):
    store, ctx = SourceEvidenceStore(tmp_path / "evidence"), context(tmp_path)
    register(store, ctx)

    async def slow(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, text="too late")

    tool = ReadSourceTool(store, resolver=resolver, request_timeout_s=.01,
                          transport=httpx.MockTransport(slow))
    assert not (await tool.run({"url": "https://example.test/article"}, ctx)).ok
    calls = []

    def loop(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "/article"})

    tool = ReadSourceTool(store, resolver=resolver, max_redirects=1,
                          transport=httpx.MockTransport(loop))
    assert not (await tool.run({"url": "https://example.test/article"}, ctx)).ok
    assert len(calls) == 2


async def test_ipv6_connect_failure_falls_back_but_http_barrier_does_not(tmp_path):
    store, ctx, calls = SourceEvidenceStore(tmp_path / "evidence"), context(tmp_path), []
    register(store, ctx)

    def handle(request):
        calls.append(request.url.host)
        if ":" in request.url.host:
            raise httpx.ConnectError("no IPv6 route", request=request)
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, text="Original text")

    addresses = lambda *args: ["2606:4700:4700::1111", "93.184.216.34"]  # noqa: E731
    tool = ReadSourceTool(store, resolver=addresses, transport=httpx.MockTransport(handle))
    assert (await tool.run({"url": "https://example.test/article"}, ctx)).ok
    assert calls == ["2606:4700:4700::1111", "93.184.216.34"]
    calls.clear()
    barrier = ReadSourceTool(store, cache_ttl_s=0, resolver=addresses,
                             transport=httpx.MockTransport(lambda request: (
                                 calls.append(request.url.host) or httpx.Response(403)
                             )))
    assert not (await barrier.run({"url": "https://example.test/article"}, ctx)).ok
    assert len(calls) == 1


async def test_out_of_document_offset_is_not_a_successful_empty_read(tmp_path):
    store, ctx = SourceEvidenceStore(tmp_path / "evidence"), context(tmp_path)
    register(store, ctx)
    tool = ReadSourceTool(store, resolver=resolver, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"Content-Type": "text/plain"}, text="Short"),
    ))
    result = await tool.run({"url": "https://example.test/article", "offset": 100}, ctx)
    assert not result.ok and "offset" in result.error


async def test_history_output_retains_valid_json_with_real_continuation(tmp_path):
    store, ctx = SourceEvidenceStore(tmp_path / "evidence"), context(tmp_path)
    payload = json.dumps([{"url": "https://example.test/" + str(index),
                           "title": "A" * 1000, "snippet": "B" * 8000}
                          for index in range(5)])
    for index in range(4):
        store.record_search("query" + str(index), ToolResult(ok=True, stdout=payload), ctx)
    result = await SourceHistoryTool(store).run({"limit": 30}, ctx)
    output = json.loads(result.stdout)
    assert len(result.stdout.encode()) <= 64000 and result.truncated
    assert output["next_offset"] == len(output["events"]) > 0
    continuation = json.loads((await SourceHistoryTool(store).run(
        {"offset": output["next_offset"]}, ctx,
    )).stdout)
    assert continuation["events"][0]["query"] != output["events"][0]["query"]


async def test_single_oversize_history_event_returns_compact_usable_index(tmp_path):
    store, ctx = SourceEvidenceStore(tmp_path / "evidence"), context(tmp_path)
    payload = json.dumps([{"url": "https://example.test/" + "a" * 3000 + str(index),
                           "title": "题" * 1000, "snippet": "证" * 8000}
                          for index in range(5)])
    store.record_search("查" * 2000, ToolResult(ok=True, stdout=payload), ctx,
                        retrieved_at="2026-01-03T00:00:00+00:00", request_id="original")
    result = await SourceHistoryTool(store).run({}, ctx)
    output = json.loads(result.stdout)
    assert result.ok and result.truncated and len(result.stdout.encode()) <= 64000
    event = output["events"][0]
    assert event["compact_source_index"] and len(event["sources"]) == 5
    assert event["request_id"] == "original" and event["retrieved_at"].startswith("2026-01-03")
    assert all(row["url"] and row["snippet_truncated"] for row in event["sources"])


async def test_compressed_response_is_rejected_before_unbounded_decoding(tmp_path):
    import gzip

    store, ctx = SourceEvidenceStore(tmp_path / "evidence"), context(tmp_path)
    register(store, ctx)

    def handle(request):
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, headers={"Content-Type": "text/plain",
                                           "Content-Encoding": "gzip"},
                              content=gzip.compress(b"x" * 1000))

    result = await ReadSourceTool(store, resolver=resolver,
                                  transport=httpx.MockTransport(handle)).run(
                                      {"url": "https://example.test/article"}, ctx,
                                  )
    assert not result.ok and "compressed" in result.error


def dns_response(request, addresses=("93.184.216.34",), **changes):
    kind = request.url.params["type"]
    assert request.url.host == "1.1.1.1" and request.url.path == "/dns-query"
    assert request.method == "GET" and request.headers["Host"] == "cloudflare-dns.com"
    assert request.extensions["sni_hostname"] == "cloudflare-dns.com"
    assert request.headers["Accept-Encoding"] == "identity"
    number = 1 if kind == "A" else 28
    return httpx.Response(200, headers={"Content-Type": "application/dns-json"}, json={
        "Status": 0, "TC": False,
        "Question": [{"name": request.url.params["name"], "type": number}],
        "Answer": [{"name": request.url.params["name"], "type": 1, "data": address}
                   for address in addresses] if kind == "A" else [], **changes,
    })


async def test_fake_system_dns_uses_verified_fixed_https_dns_and_records_provenance(tmp_path):
    store, ctx, connections = SourceEvidenceStore(tmp_path / "evidence"), context(tmp_path), []
    register(store, ctx)

    def page(request):
        connections.append(request.url.host)
        return httpx.Response(200, headers={"Content-Type": "text/plain"},
                              text="Original public page")

    tool = ReadSourceTool(store, resolver=lambda *args: ["198.18.0.101"],
                          dns_transport=httpx.MockTransport(dns_response),
                          transport=httpx.MockTransport(page))
    result = await tool.run({"url": "https://example.test/article"}, ctx)
    assert result.ok and connections == ["93.184.216.34"]
    network = json.loads(result.stdout)["network_provenance"][0]
    assert network["dns"]["method"] == "public_https_dns_fallback"
    assert network["dns"]["tls_verified"] and network["dns"]["resolver_ip"] == "1.1.1.1"
    assert len(network["dns"]["responses"]) == 2
    assert all(row["response_sha256"] for row in network["dns"]["responses"])


@pytest.mark.parametrize("changes,addresses", [
    ({}, ("198.18.0.1",)), ({}, ("93.184.216.34", "10.1.2.3")),
    ({"Status": 3}, ()), ({"TC": True}, ("93.184.216.34",)),
    ({"Question": [{"name": "different.test", "type": 1}]}, ("93.184.216.34",)),
])
async def test_dns_fallback_rejects_private_or_mismatched_answers(tmp_path, changes, addresses):
    store, ctx, calls = SourceEvidenceStore(tmp_path / "evidence"), context(tmp_path), []
    register(store, ctx)
    tool = ReadSourceTool(store, resolver=lambda *args: ["198.18.0.1"],
                          dns_transport=httpx.MockTransport(
                              lambda request: dns_response(request, addresses, **changes)),
                          transport=httpx.MockTransport(lambda request: calls.append(request)))
    result = await tool.run({"url": "https://example.test/article"}, ctx)
    assert not result.ok and calls == []


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"Location": "http://127.0.0.1/private"}),
    httpx.Response(403), httpx.Response(200, headers={"Content-Type": "text/html"}, text="barrier"),
    httpx.Response(200, headers={"Content-Type": "application/json"}, content=b"x" * 16385),
])
async def test_dns_fallback_does_not_follow_redirect_or_barrier(tmp_path, response):
    store, ctx, calls = SourceEvidenceStore(tmp_path / "evidence"), context(tmp_path), []
    register(store, ctx)
    tool = ReadSourceTool(store, resolver=lambda *args: ["198.18.0.1"],
                          dns_transport=httpx.MockTransport(lambda request: response),
                          transport=httpx.MockTransport(lambda request: calls.append(request)))
    assert not (await tool.run({"url": "https://example.test/article"}, ctx)).ok
    assert not calls


async def test_arxiv_variant_requires_registered_same_id_and_version(tmp_path):
    store, ctx = SourceEvidenceStore(tmp_path / "evidence"), context(tmp_path)
    original = "https://arxiv.org/pdf/2501.12345v2.pdf"
    register(store, ctx, original)
    tool = ReadSourceTool(store, resolver=resolver, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"Content-Type": "text/html"},
                                      text="<h4>Results</h4><table><tr><td>100</td><td>USD</td>"
                                           "</tr></table>"),
    ))
    result = await tool.run({"url": "https://arxiv.org/html/2501.12345v2"}, ctx)
    page = json.loads(result.stdout)
    assert result.ok and page["variant_of"] == original
    assert page["derived_format"] == "arxiv_same_document_html"
    assert page["source_request_id"] == "old-real-request"
    assert "100 |" in page["text"] and "100USD" not in page["text"]
    for url in ("https://arxiv.org/html/2501.12345v3", "https://arxiv.org/html/2501.12345",
                "https://arxiv.org/abs/2501.99999v2", "https://other.test/html/2501.12345v2",
                "https://arxiv.org/html/2501.12345v2/extra", "https://arxiv.org/search?q=a"):
        assert not (await tool.run({"url": url}, ctx)).ok
