"""Provider-usage provenance and durable metadata for all model-call paths."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from moumou.adapters import DeepSeekClient, FakeLLM
from moumou.adapters.llm import LLMResponse
from moumou.context.builder import BOARD_HEADER, LESSONS_HEADER
from moumou.eval.telemetry import EvalLLM, read_telemetry, summarize_calls


def response(tokens_in=10, tokens_out=3, *, cache=True):
    return LLMResponse(
        content="done", tokens_in=tokens_in, tokens_out=tokens_out,
        usage_present=True,
        usage_raw={"prompt_tokens": tokens_in, "completion_tokens": tokens_out,
                   **({"prompt_cache_hit_tokens": 2} if cache else {})},
    )


class RealClient:
    api_key = "secret-never-written"
    model = "local-http-stub"

    async def chat(self, messages, tools=None):
        return response()


async def test_all_call_paths_are_counted_without_prompt_or_secret_persistence(tmp_path):
    path = tmp_path / "telemetry.json"
    wrapped = EvalLLM(RealClient(), path)
    assert wrapped.api_key == RealClient.api_key
    assert wrapped.model == "local-http-stub"
    messages = [{"role": "user", "content": "secret prompt"}]
    await wrapped.chat(messages)
    await wrapped.chat(messages, [{"function": {"name": "submit_lesson"}}])
    wrapped.set_phase("post_maintenance")
    await wrapped.chat(messages, [{"function": {"name": "submit_revision"}}])
    saved = read_telemetry(path)
    assert [row["kind"] for row in saved["calls"]] == ["agent", "reflection", "role_review"]
    summary = saved["summary"]
    assert summary["calls_total"] == summary["usage_measured_calls"] == 3
    assert summary["total_tokens_in"] == 30
    assert summary["total_tokens_out"] == 9
    assert summary["total_cache_hit_tokens"] == 6
    assert summary["usage_missing_calls"] == 0
    assert summary["by_phase_calls"] == {"execution": 2, "post_maintenance": 1}
    assert summary["by_phase"]["post_maintenance"]["total_tokens_in"] == 10
    assert summary["by_kind"]["reflection"]["calls_total"] == 1
    text = path.read_text(encoding="utf-8")
    assert "secret prompt" not in text
    assert RealClient.api_key not in text
    assert "done" not in text


async def test_missing_usage_is_not_a_measured_zero(tmp_path):
    class Missing(RealClient):
        async def chat(self, messages, tools=None):
            return LLMResponse(content="unreported")

    wrapped = EvalLLM(Missing(), tmp_path / "telemetry.json")
    await wrapped.chat([])
    assert wrapped.summary()["total_tokens_in"] is None
    assert wrapped.summary()["usage_missing_calls"] == 1
    assert wrapped.calls[0]["tokens_in"] is None


async def test_real_explicit_zero_is_measured_but_fake_tokens_are_not():
    class Zero(RealClient):
        async def chat(self, messages, tools=None):
            return response(0, 0)

    real = EvalLLM(Zero())
    await real.chat([])
    assert real.summary()["total_tokens_in"] == 0
    assert real.summary()["usage_missing_calls"] == 0
    fake = EvalLLM(FakeLLM([response()]))
    await fake.chat([])
    assert fake.calls[0]["measurement_source"] == "offline_scripted"
    assert fake.summary()["total_tokens_in"] is None
    assert fake.summary()["usage_missing_calls"] == 1


async def test_missing_cache_usage_does_not_fabricate_a_cache_miss():
    class NoCache(RealClient):
        async def chat(self, messages, tools=None):
            return response(cache=False)

    wrapped = EvalLLM(NoCache())
    await wrapped.chat([])
    assert wrapped.summary()["total_tokens_in"] == 10
    assert wrapped.summary()["total_cache_hit_tokens"] is None


async def test_known_memory_requires_exact_lesson_inside_system_lessons_section(tmp_path):
    lesson = "Quote file paths before calling the shell."
    wrapped = EvalLLM(RealClient(), tmp_path / "telemetry.json", memory_records=[
        {"id": "history", "lesson": lesson},
    ])
    await wrapped.chat([{"role": "system", "content": "role" + LESSONS_HEADER + "- " + lesson}])
    assert wrapped.summary()["known_memory_ids"] == ["history"]
    hit = wrapped.calls[0]["memory"]["known_memories"][0]
    assert hit["id"] == "history"
    assert len(hit["lesson_sha256"]) == 64
    saved = (tmp_path / "telemetry.json").read_text(encoding="utf-8")
    assert lesson not in saved
    await wrapped.chat([{"role": "user", "content": LESSONS_HEADER + "- " + lesson}])
    assert wrapped.calls[1]["memory"]["known_memories"] == []
    await wrapped.chat([{"role": "system", "content": LESSONS_HEADER + "- other" + BOARD_HEADER
                         + "- " + lesson}])
    assert wrapped.calls[2]["memory"]["known_memories"] == []
    await wrapped.chat([{"role": "system", "content": LESSONS_HEADER + "- Don't " + lesson}])
    assert wrapped.calls[3]["memory"]["known_memories"] == []


async def test_call_in_flight_is_saved_before_waiting_and_cancelled_without_error_text(tmp_path):
    entered = asyncio.Event()

    class Waiting(RealClient):
        async def chat(self, messages, tools=None):
            entered.set()
            await asyncio.Future()

    path = tmp_path / "telemetry.json"
    wrapped = EvalLLM(Waiting(), path)
    task = asyncio.create_task(wrapped.chat([]))
    await entered.wait()
    assert read_telemetry(path)["summary"]["calls_in_flight"] == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = read_telemetry(path)
    assert saved["calls"][0]["status"] == "cancelled"
    assert saved["summary"]["calls_failed"] == 1
    assert saved["summary"]["total_tokens_in"] is None


async def test_client_error_is_preserved_but_its_secret_message_is_not_saved(tmp_path):
    class Failing(RealClient):
        async def chat(self, messages, tools=None):
            raise RuntimeError(self.api_key)

    path = tmp_path / "telemetry.json"
    wrapped = EvalLLM(Failing(), path)
    with pytest.raises(RuntimeError, match=RealClient.api_key):
        await wrapped.chat([])
    saved = read_telemetry(path)
    assert saved["calls"][0]["error_type"] == "RuntimeError"
    assert RealClient.api_key not in path.read_text(encoding="utf-8")
    assert saved["summary"]["usage_missing_calls"] == 1


async def test_concurrent_calls_keep_stable_unique_identities_and_all_usage(tmp_path):
    class Concurrent(RealClient):
        async def chat(self, messages, tools=None):
            await asyncio.sleep(0)
            return response()

    wrapped = EvalLLM(Concurrent(), tmp_path / "telemetry.json")
    await asyncio.gather(*(wrapped.chat([]) for _ in range(4)))
    saved = read_telemetry(wrapped.path)
    assert [row["call_id"] for row in saved["calls"]] == [1, 2, 3, 4]
    assert saved["summary"]["total_tokens_in"] == 40
    assert saved["summary"]["calls_in_flight"] == 0


@pytest.mark.parametrize("usage,present", [
    (None, False), ({}, False), ({"prompt_tokens": 0}, False),
    ({"prompt_tokens": 0, "completion_tokens": 0}, True),
    ({"prompt_tokens": 10, "completion_tokens": 3, "prompt_cache_hit_tokens": 2}, True),
    ({"prompt_tokens": True, "completion_tokens": 2}, False),
])
def test_parse_keeps_legacy_budgets_and_explicit_usage_provenance(usage, present):
    client = DeepSeekClient(api_key="offline-placeholder")
    parsed = client._parse({"choices": [{"message": {"content": "ok"}}], "usage": usage})
    assert parsed.usage_present is present
    assert parsed.tokens_in == int((usage or {}).get("prompt_tokens") or 0)
    assert parsed.tokens_out == int((usage or {}).get("completion_tokens") or 0)
    assert all(type(value) is int and value >= 0 for value in parsed.usage_raw.values())


def test_provider_unknown_usage_keys_are_not_persisted():
    client = DeepSeekClient(api_key="offline-placeholder")
    parsed = client._parse({"usage": {"prompt_tokens": 1, "completion_tokens": 2,
                                     "api_key": "malformed-provider-secret"}})
    assert parsed.usage_raw == {"prompt_tokens": 1, "completion_tokens": 2}
    assert "malformed-provider-secret" not in parsed.model_dump_json()


@pytest.mark.parametrize("records", [[{"id": "x", "lesson": ""}],
                                      [{"id": "x", "lesson": "one"},
                                       {"id": "x", "lesson": "two"}]])
def test_memory_identity_ambiguity_is_rejected(records):
    with pytest.raises(ValueError):
        EvalLLM(RealClient(), memory_records=records)


def test_memory_records_accept_the_public_record_shape():
    wrapped = EvalLLM(RealClient(), memory_records=[SimpleNamespace(id="x", lesson="known")])
    wrapped.set_memory_records([])
    assert wrapped.summary()["known_memory_ids"] == []


def test_invalid_snapshot_is_not_interpreted_as_zero_calls(tmp_path):
    path = tmp_path / "telemetry.json"
    path.write_text(json.dumps({"schema_version": 7, "calls": [], "summary": {}}), encoding="utf-8")
    with pytest.raises(ValueError):
        read_telemetry(path)


async def test_partial_usage_preserves_known_input_without_claiming_output_cost():
    class Partial(RealClient):
        async def chat(self, messages, tools=None):
            return LLMResponse(usage_present=False, usage_raw={"prompt_tokens": 8})

    wrapped = EvalLLM(Partial())
    await wrapped.chat([])
    summary = wrapped.summary()
    assert summary["known_tokens_in"] == summary["total_tokens_in"] == 8
    assert summary["total_tokens_out"] is None
    assert summary["usage_missing_calls"] == 1


def _call_snapshot(*, running=False):
    return {
        "call_id": 1, "phase": "execution", "kind": "agent",
        "started_at": "2026-11-13T00:00:00+00:00",
        "finished_at": None if running else "2026-11-13T00:00:01+00:00",
        "elapsed_s": None if running else 1.0,
        "status": "running" if running else "succeeded", "error_type": None,
        "measurement_source": "provider_response",
        "usage_present": None if running else True,
        "tokens_in": None if running else 10, "tokens_out": None if running else 3,
        "cache_hit_tokens": None,
        "memory": {"lessons_header_present": False, "known_memories": []},
    }


def _save_snapshot(path, calls, *, summary=None):
    path.write_text(json.dumps({
        "schema_version": 1, "calls": calls,
        "summary": summarize_calls(calls) if summary is None else summary,
    }), encoding="utf-8")


def test_read_rejects_fake_zero_usage_in_an_otherwise_valid_snapshot(tmp_path):
    path = tmp_path / "telemetry.json"
    calls = [_call_snapshot()]
    summary = summarize_calls(calls)
    summary["total_tokens_in"] = 0
    _save_snapshot(path, calls, summary=summary)
    with pytest.raises(ValueError, match="disagrees"):
        read_telemetry(path)


def test_read_accepts_valid_inflight_coverage_without_a_client_or_event_loop(tmp_path, monkeypatch):
    def no_client(*args, **kwargs):
        raise AssertionError("reading telemetry must not initialize a client")

    monkeypatch.setattr(EvalLLM, "__init__", no_client)
    path = tmp_path / "telemetry.json"
    _save_snapshot(path, [_call_snapshot(running=True)])
    saved = read_telemetry(path)
    assert saved["summary"]["calls_in_flight"] == saved["summary"]["usage_missing_calls"] == 1
    assert saved["summary"]["total_tokens_in"] is None
    saved["summary"]["total_tokens_in"] = 0
    path.write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(ValueError, match="disagrees"):
        read_telemetry(path)


@pytest.mark.parametrize("key,value", [
    ("tokens_in", -1), ("elapsed_s", float("nan")), ("elapsed_s", -1),
    ("started_at", "2026-11-13T00:00:00"), ("call_id", True), ("status", "unknown"),
    ("memory", {"lessons_header_present": True, "known_memories": [
        {"id": "known", "lesson_sha256": "broken"},
    ]}),
])
def test_read_rejects_malformed_calls_even_when_the_summary_is_present(tmp_path, key, value):
    path = tmp_path / "telemetry.json"
    calls = [_call_snapshot()]
    summary = summarize_calls(calls)
    calls[0][key] = value
    _save_snapshot(path, calls, summary=summary)
    with pytest.raises(ValueError):
        read_telemetry(path)


def test_read_rejects_duplicate_call_ids_even_when_the_totals_match(tmp_path):
    path = tmp_path / "telemetry.json"
    _save_snapshot(path, [_call_snapshot(), _call_snapshot()])
    with pytest.raises(ValueError, match="identity"):
        read_telemetry(path)


def test_read_rejects_duplicate_json_fields_and_boolean_summary_counts(tmp_path):
    path = tmp_path / "telemetry.json"
    path.write_text('{"schema_version": 1, "calls": [], "calls": [], "summary": {}}',
                    encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        read_telemetry(path)
    calls = [_call_snapshot()]
    summary = summarize_calls(calls)
    summary["calls_total"] = True
    _save_snapshot(path, calls, summary=summary)
    with pytest.raises(ValueError, match="disagrees"):
        read_telemetry(path)


@pytest.mark.parametrize("messages,tools", [
    ([{"role": "system", "content": "planner"}, {"role": "user", "content": "task"}],
     [{"function": {"name": "submit_revision"}}]),
    ([{"role": "user", "content": "task"}],
     [{"function": {"name": "submit_revision"}}, {"function": {"name": "read_file"}}]),
])
async def test_agent_tool_name_collision_is_not_a_maintenance_call(messages, tools):
    wrapped = EvalLLM(RealClient())
    await wrapped.chat(messages, tools)
    assert wrapped.calls[0]["kind"] == "agent"
    assert wrapped.summary()["by_kind"]["agent"]["calls_total"] == 1
