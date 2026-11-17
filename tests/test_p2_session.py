"""P2 stages explicit answers; existing P3 alone consumes or judges them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from moumou.contracts import HumanRequest, HumanResponse
from moumou.ui.session import FileSession, SessionNotReady, StaleRequest


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def make_session(tmp_path, *, kind="question", options=(), dataset="instructor"):
    root = tmp_path / "run"
    folder = root / "scenario_1"
    for name in ("requests", "pending", "responses", "consumed"):
        (folder / "human" / name).mkdir(parents=True)
    write_json(root / "test.json", {
        "schema_version": 1, "kind": "instructor_test", "dataset_kind": dataset,
        "human_mode": "files", "status": "running",
        "scenarios": [{"scenario_id": "scenario_1"}],
    })
    write_json(root / "active.json", {
        "scenario_id": "scenario_1", "phase": "task_1", "scenario_root": str(folder),
        "human_pending": str(folder / "human/pending"), "manifest": str(root / "test.json"),
    })
    request = HumanRequest(id="original-question", task_id="t1", agent_id="lead",
                           kind=kind, summary="Actual original fixture request",
                           project_id="scenario_1", conversation_id="instructor-group",
                           role="lead", options=list(options))
    record = {"phase": "task_1", "created_at": "2026-11-16T12:00:00+00:00",
              "request": request.model_dump(mode="json")}
    for directory in ("requests", "pending"):
        write_json(folder / "human" / directory / (request.id + ".json"), record)
    client = FileSession(root)
    return root, client, client.pending()[0]


@pytest.mark.parametrize("dataset", ["instructor", "supplementary"])
def test_explicit_answer_is_atomically_staged_without_consuming_or_rewriting_evidence(
        tmp_path, dataset):
    root, client, item = make_session(tmp_path, dataset=dataset)
    archive = root / "scenario_1/human/requests/original-question.json"
    pending = root / "scenario_1/human/pending/original-question.json"
    before = archive.read_bytes(), pending.read_bytes(), (root / "test.json").read_bytes()
    result = client.stage_response(item, HumanResponse(request_id=item.request_id,
                                                       text="Retain the original records"))
    assert result.state == "staged"
    assert read_json(Path(result.path))["text"] == "Retain the original records"
    assert (archive.read_bytes(), pending.read_bytes(), (root / "test.json").read_bytes()) == before
    assert not (root / "scenario_1/human/consumed/original-question.json").exists()
    assert [path.name for path in (root / "scenario_1/human/responses").iterdir()] == [
        "original-question.json",
    ]
    assert client.pending() == [], "a staged answer must not be offered for another answer"


@pytest.mark.parametrize("damage", ["wrong_id", "empty", "outside_options", "question_approval"])
def test_invalid_answers_do_not_publish_any_file(tmp_path, damage):
    root, client, item = make_session(tmp_path, options=("keep", "cancel"))
    payload = {"request_id": item.request_id, "text": "keep"}
    if damage == "wrong_id":
        payload["request_id"] = "another-question"
    elif damage == "empty":
        payload["text"] = "   "
    elif damage == "outside_options":
        payload["text"] = "invented"
    else:
        payload["decision"] = "approve"
    with pytest.raises(ValueError):
        client.stage_response(item, HumanResponse(**payload))
    assert list((root / "scenario_1/human/responses").iterdir()) == []


@pytest.mark.parametrize("kind", ["approval", "review"])
def test_permission_or_quality_requires_explicit_decision_and_keeps_its_original_kind(
        tmp_path, kind):
    root, client, item = make_session(tmp_path, kind=kind, options=("approve", "reject"))
    with pytest.raises(ValueError, match="explicit"):
        client.stage_response(item, HumanResponse(request_id=item.request_id, text="looks fine"))
    result = client.stage_response(item, HumanResponse(request_id=item.request_id,
                                                       decision="reject", text="Actual note"))
    assert result.state == "staged"
    assert read_json(Path(result.path))["decision"] == "reject"
    assert read_json(root / "scenario_1/human/requests/original-question.json")["request"][
        "kind"] == kind
    assert read_json(root / "test.json")["status"] == "running"


@pytest.mark.parametrize("directory,state", [("responses", "already_staged"),
                                             ("consumed", "already_consumed")])
def test_existing_answer_is_never_overwritten(tmp_path, directory, state):
    root, client, item = make_session(tmp_path)
    existing = root / "scenario_1/human" / directory / (item.request_id + ".json")
    write_json(
        existing, HumanResponse(request_id=item.request_id, text="first operator").model_dump()
    )
    before = existing.read_bytes()
    result = client.stage_response(item, HumanResponse(request_id=item.request_id, text="second"))
    assert result.state == state and existing.read_bytes() == before


def test_publication_race_retains_the_other_ui_response(tmp_path, monkeypatch):
    root, client, item = make_session(tmp_path)
    target = root / "scenario_1/human/responses/original-question.json"

    def other_operator_wins(source, destination):
        assert Path(destination) == target
        write_json(target, HumanResponse(request_id=item.request_id, text="other UI").model_dump())
        raise FileExistsError(target)

    monkeypatch.setattr("moumou.ui.session.os.link", other_operator_wins)
    result = client.stage_response(item, HumanResponse(request_id=item.request_id, text="this UI"))
    assert result.state == "already_staged" and read_json(target)["text"] == "other UI"
    assert [path.name for path in target.parent.iterdir()] == [target.name]


@pytest.mark.parametrize("damage", ["phase", "original_body", "pending_body"])
def test_stale_scope_or_changed_original_request_is_rejected_before_publication(tmp_path, damage):
    root, client, item = make_session(tmp_path)
    if damage == "phase":
        active = read_json(root / "active.json")
        active["phase"] = "task_2"
        write_json(root / "active.json", active)
    else:
        directory = "requests" if damage == "original_body" else "pending"
        path = root / "scenario_1/human" / directory / (item.request_id + ".json")
        record = read_json(path)
        record["request"]["summary"] = "Replaced question"
        write_json(path, record)
    with pytest.raises((ValueError, StaleRequest)):
        client.stage_response(item, HumanResponse(request_id=item.request_id, text="old answer"))
    assert list((root / "scenario_1/human/responses").iterdir()) == []


def test_active_scope_change_during_temporary_write_is_rechecked(tmp_path, monkeypatch):
    root, client, item = make_session(tmp_path)
    original = client._recheck
    calls = 0

    def change_on_second_check(value):
        nonlocal calls
        calls += 1
        if calls == 2:
            active = read_json(root / "active.json")
            active["phase"] = "task_2"
            write_json(root / "active.json", active)
        return original(value)

    monkeypatch.setattr(client, "_recheck", change_on_second_check)
    with pytest.raises(StaleRequest):
        client.stage_response(item, HumanResponse(request_id=item.request_id, text="answer"))
    assert list((root / "scenario_1/human/responses").iterdir()) == []


@pytest.mark.parametrize("damage", ["console", "scenario_traversal", "pointer_traversal",
                                   "pointer_outside", "unsafe_request_id"])
def test_untrusted_session_paths_or_wrong_transport_cannot_be_used(tmp_path, damage):
    root, _, item = make_session(tmp_path)
    if damage == "console":
        value = read_json(root / "test.json")
        value["human_mode"] = "console"
        write_json(root / "test.json", value)
    elif damage == "unsafe_request_id":
        pending = root / "scenario_1/human/pending" / (item.request_id + ".json")
        pending.rename(pending.with_name("bad.id.json"))
    else:
        active = read_json(root / "active.json")
        if damage == "scenario_traversal":
            active["scenario_id"] = "../other"
        elif damage == "pointer_traversal":
            active["scenario_root"] = str(root / "scenario_1/../scenario_1")
        else:
            active["human_pending"] = str(tmp_path / "outside")
        write_json(root / "active.json", active)
    with pytest.raises(ValueError):
        FileSession(root).pending()


def test_symlinked_response_directory_is_not_a_write_escape(tmp_path):
    root, client, item = make_session(tmp_path)
    responses = root / "scenario_1/human/responses"
    responses.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        responses.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("host does not permit directory symlinks")
    with pytest.raises(ValueError, match="alias|escapes"):
        client.stage_response(item, HumanResponse(request_id=item.request_id, text="answer"))
    assert list(outside.iterdir()) == []


def test_inflight_consumed_record_stays_unknown_until_validated(tmp_path):
    root, client, item = make_session(tmp_path)
    consumed = root / "scenario_1/human/consumed/original-question.json"
    consumed.write_bytes(b'{"request_id":')
    with pytest.raises(SessionNotReady, match="incomplete"):
        client.stage_response(item, HumanResponse(request_id=item.request_id, text="answer"))
    assert list((root / "scenario_1/human/responses").iterdir()) == []
    consumed.write_bytes(b'{"invalid":\n')
    with pytest.raises(ValueError):
        client.pending()


def test_pending_disappearing_during_read_is_a_normal_consumption_race(tmp_path, monkeypatch):
    _, client, item = make_session(tmp_path)
    read = client._read

    def disappeared(path, *args, **kwargs):
        if path.parent.name == "pending":
            raise FileNotFoundError(path)
        return read(path, *args, **kwargs)

    monkeypatch.setattr(client, "_read", disappeared)
    assert client.pending() == []
    assert client.stage_response(
        item, HumanResponse(request_id=item.request_id, text="answer")
    ).state == (
        "no_longer_pending"
    )


def test_history_uses_real_source_labels_and_defers_only_unfinished_tail(tmp_path):
    root, client, _ = make_session(tmp_path)
    journal = root / "scenario_1/dialogue.jsonl"
    journal.write_bytes(b'{"event":"human_request","phase":"task_1"}\n{"event":')
    rows = client.history()
    assert len(rows) == 1 and rows[0].source == "scenario_1/dialogue.jsonl"
    assert rows[0].line_number == 1 and rows[0].record["event"] == "human_request"
    journal.write_bytes(b'{"event":\n')
    with pytest.raises(ValueError):
        client.history()
