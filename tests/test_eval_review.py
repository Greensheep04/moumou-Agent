"""Saved-delivery human review regressions. Authored without executing this change."""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_eval_session_report import session

from moumou.eval import review as review_module
from moumou.eval.report import build_report
from moumou.eval.review import review_binding, review_task


def saved_case(root, *, status="DONE", machine=None):
    task, path = session(root)
    task["root_status"] = status
    task["nodes"][0]["status"] = status
    task["root_machine_accepted"] = machine
    path.write_text(json.dumps(task), encoding="utf-8")
    manifest_path = root / "test.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["started_at"] = "2026-11-16T01:00:00+00:00"
    manifest["code"] = {"source_sha256": "d" * 64}
    manifest["scenarios"][0]["source_sha256"] = {
        "readme": "a" * 64, "roles": "b" * 64, "task1": "c" * 64,
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return task, path, manifest_path


async def pending_review(root, *, supersedes=None):
    operation = asyncio.create_task(review_task(
        root, "scenario_1", "task_1", human_mode="files", supersedes=supersedes,
    ))
    for _ in range(200):
        pending = list((root / "reviews").glob("*/human/pending/*.json"))
        if pending:
            path = pending[-1]
            return operation, path, json.loads(path.read_text(encoding="utf-8"))["request"]
        if operation.done():
            await operation
        await asyncio.sleep(0.01)
    operation.cancel()
    await asyncio.gather(operation, return_exceptions=True)
    raise AssertionError("fixture human review did not publish its pending request")


async def answer_review(root, decision, *, supersedes=None):
    operation, pending, request = await pending_review(root, supersedes=supersedes)
    response = pending.parent.parent / "responses" / pending.name
    response.write_text(json.dumps({
        "request_id": request["id"], "decision": decision,
        "text": "Synthetic test-only human response; not an instructor evaluation score",
    }), encoding="utf-8")
    path = await asyncio.wait_for(operation, 5)
    return path, json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("decision,success", [("approve", True), ("reject", False)])
async def test_file_review_derives_quality_without_changing_raw_results(
    tmp_path, decision, success,
):
    _, path, manifest = saved_case(tmp_path)
    before_result, before_manifest = path.read_bytes(), manifest.read_bytes()
    review_path, review = await answer_review(tmp_path, decision)
    report = build_report(tmp_path)
    row = report["records"][0]
    assert row["verified_goal_success"] is success
    assert row["raw_quality"]["verified_goal_success"] is None
    assert row["quality_source"] == "offline_human_review"
    assert row["review"]["path"] == str(review_path)
    assert row["review"]["review_id"] == review["review_id"]
    assert row["review"]["identity_limit"] == (
        "record consistency, not cryptographic human authentication"
    )
    assert report["metrics"]["planned_root_goals"] == 6
    assert report["metrics"]["not_delivered"] == 5
    assert report["metrics"]["known_unsuccessful"] == (decision == "reject")
    assert path.read_bytes() == before_result and manifest.read_bytes() == before_manifest


@pytest.mark.parametrize("status,machine", [("DEAD", None), ("DONE", False)])
async def test_human_approval_cannot_repair_agent_or_machine_failure(tmp_path, status, machine):
    saved_case(tmp_path, status=status, machine=machine)
    await answer_review(tmp_path, "approve")
    model = build_report(tmp_path)
    assert model["records"][0]["root_status"] == status
    assert model["records"][0]["verified_goal_success"] is False
    assert model["metrics"]["verified_successes"] == 0


async def test_reconsideration_requires_explicit_tip_and_retains_old_decision(tmp_path):
    saved_case(tmp_path)
    first_path, first = await answer_review(tmp_path, "approve")
    first_bytes = first_path.read_bytes()
    with pytest.raises(ValueError, match="supersedes"):
        await review_task(tmp_path, "scenario_1", "task_1", human_mode="files")
    _, second = await answer_review(tmp_path, "reject", supersedes=first["review_id"])
    row = build_report(tmp_path)["records"][0]
    assert row["verified_goal_success"] is False
    assert [r["review_id"] for r in row["review"]["history"]] == [
        first["review_id"], second["review_id"],
    ]
    assert len(list((tmp_path / "reviews").glob("*/review.json"))) == 2
    assert first_path.read_bytes() == first_bytes


@pytest.mark.parametrize("change", ["result", "source", "response"])
async def test_stale_or_tampered_review_evidence_is_rejected(tmp_path, change):
    task, path, manifest_path = saved_case(tmp_path)
    review_path, review = await answer_review(tmp_path, "approve")
    if change == "result":
        task["elapsed_s"] = 13
        path.write_text(json.dumps(task), encoding="utf-8")
    elif change == "source":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["scenarios"][0]["source_sha256"]["task1"] = "e" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    else:
        intent = json.loads((review_path.parent / "intent.json").read_text(encoding="utf-8"))
        response = review_path.parent / "human/consumed" / (intent["request_id"] + ".json")
        payload = json.loads(response.read_text(encoding="utf-8"))
        payload["decision"] = "reject"
        response.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="(mismatch|decision)"):
        build_report(tmp_path)
    assert review["response"]["decision"] == "approve"


async def test_an_unanswered_or_invalid_decision_never_becomes_a_quality_pass(tmp_path):
    saved_case(tmp_path)
    operation, pending, request = await pending_review(tmp_path)
    assert build_report(tmp_path)["metrics"]["unreviewed_deliveries"] == 1
    response = pending.parent.parent / "responses" / pending.name
    response.write_text(json.dumps({"request_id": request["id"], "text": "looks good"}),
                        encoding="utf-8")
    with pytest.raises(ValueError, match="explicit decision"):
        await asyncio.wait_for(operation, 5)
    assert not list((tmp_path / "reviews").glob("*/review.json"))
    assert build_report(tmp_path)["metrics"]["verified_successes"] == 0


async def test_result_change_during_real_wait_does_not_publish_review(tmp_path):
    task, path, _ = saved_case(tmp_path)
    operation, pending, request = await pending_review(tmp_path)
    task["elapsed_s"] = 14
    path.write_text(json.dumps(task), encoding="utf-8")
    response = pending.parent.parent / "responses" / pending.name
    response.write_text(json.dumps({"request_id": request["id"], "decision": "approve"}),
                        encoding="utf-8")
    with pytest.raises(ValueError, match="changed while"):
        await asyncio.wait_for(operation, 5)
    assert not list((tmp_path / "reviews").glob("*/review.json"))


def test_arbitrary_review_json_without_human_port_evidence_is_not_accepted(tmp_path):
    task, path, manifest_path = saved_case(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    identity = "f" * 32
    folder = tmp_path / "reviews" / identity
    folder.mkdir(parents=True)
    record = {
        "schema_version": 1, "kind": "offline_quality_review", "review_id": identity,
        "reviewed_at": "2026-11-16T02:00:00+00:00", "supersedes": None,
        "binding": review_binding(tmp_path, manifest, manifest["scenarios"][0], task, path),
        "response": {"request_id": "a" * 12, "decision": "approve", "text": None},
    }
    (folder / "review.json").write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises((ValueError, FileNotFoundError)):
        build_report(tmp_path)


async def test_console_review_requires_explicit_input_and_records_optional_note(
    tmp_path, monkeypatch,
):
    saved_case(tmp_path)
    replies = iter(["", "approve fixture reviewer note"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(replies))
    path = await review_task(tmp_path, "scenario_1", "task_1", human_mode="console")
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["response"]["decision"] == "approve"
    assert record["response"]["text"] == "fixture reviewer note"


async def test_unstarted_task_cannot_get_an_offline_review(tmp_path):
    saved_case(tmp_path)
    with pytest.raises(ValueError, match="unstarted or undelivered"):
        await review_task(tmp_path, "scenario_1", "task_2", human_mode="files")
    assert not (tmp_path / "reviews").exists()


def test_concurrent_review_publishers_cannot_both_create_a_chain_root(tmp_path, monkeypatch):
    saved_case(tmp_path)
    publishing, release = threading.Event(), threading.Event()
    original_write = review_module._new_json

    def pause_first_publication(path, value):
        if value.get("kind") == "offline_quality_review" and not publishing.is_set():
            publishing.set()
            if not release.wait(5):
                raise RuntimeError("fixture publication was not released")
        return original_write(path, value)

    def review_in_independent_loop():
        return asyncio.run(review_task(tmp_path, "scenario_1", "task_1", human_mode="console"))

    monkeypatch.setattr(review_module, "_new_json", pause_first_publication)
    monkeypatch.setattr("builtins.input", lambda prompt: "approve fixture-only concurrent review")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(review_in_independent_loop)
        try:
            assert publishing.wait(5), "first review never reached its final publication"
            second = pool.submit(review_in_independent_loop)
            with pytest.raises(ValueError, match="publication is in progress"):
                second.result(timeout=5)
        finally:
            release.set()
        first.result(timeout=5)
    assert len(list((tmp_path / "reviews").glob("*/review.json"))) == 1
    assert not (tmp_path / "reviews/.publish.lock").exists()
    assert build_report(tmp_path)["metrics"]["verified_successes"] == 1


def test_publication_lock_does_not_remove_a_replacement_owned_by_another_writer(tmp_path):
    (tmp_path / "reviews").mkdir()
    lock = tmp_path / "reviews/.publish.lock"
    with review_module._publication_lock(tmp_path):
        lock.unlink()
        lock.write_bytes(b"replacement lock belongs to another writer")
    assert lock.read_bytes() == b"replacement lock belongs to another writer"
