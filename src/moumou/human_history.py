"""Scoped, recorded human-port clarification facts, never reusable tool grants."""

from __future__ import annotations

import json
from datetime import datetime

from moumou.contracts.human import HumanRequest, HumanResponse


class HumanExchangeError(ValueError):
    """A recorded question cannot be safely recovered for this task and role."""


def exchange_scope(node) -> dict[str, str]:
    return {
        "project_id": node.project_id,
        "conversation_id": node.conversation_id,
        "task_id": node.id,
        "role": node.role,
    }


def _recorded_timestamp(value, field):
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing human-exchange {field}")
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError(f"human-exchange {field} requires a timezone")


def validate_exchange(record, node, *, require_answered=True) -> dict:
    """Validate provenance without substituting an empty or affirmative answer."""
    try:
        if not isinstance(record, dict) or type(record.get("schema_version")) is not int:
            raise ValueError("missing human-exchange schema version")
        if record["schema_version"] != 1:
            raise ValueError("unsupported human-exchange schema version")
        for key, expected in exchange_scope(node).items():
            if not expected or record.get(key) != expected:
                raise ValueError(f"human-exchange scope mismatch: {key}")
        request = HumanRequest.model_validate(record["request"])
        if request.model_dump(mode="json") != record["request"]:
            raise ValueError("recorded request is incomplete or has unknown fields")
        if request.kind != "question":
            raise ValueError("only clarification questions belong in this history")
        if not request.id.strip() or not request.summary.strip():
            raise ValueError("recorded question requires an ID and non-empty question")
        for key, expected in exchange_scope(node).items():
            actual = request.task_id if key == "task_id" else getattr(request, key)
            if actual != expected:
                raise ValueError(f"recorded request scope mismatch: {key}")
        if not request.agent_id.strip() or record.get("agent_id") != request.agent_id:
            raise ValueError("recorded request agent provenance mismatch")
        _recorded_timestamp(record.get("asked_at"), "request timestamp")
        payload = record.get("response")
        if payload is None:
            if record.get("answered_at") is not None:
                raise ValueError("answered timestamp without a recorded response")
            if require_answered:
                raise HumanExchangeError(f"human_exchange_pending:{request.id}")
        else:
            response = HumanResponse.model_validate(payload)
            if response.model_dump(mode="json") != payload:
                raise ValueError("recorded response is incomplete or has unknown fields")
            if response.request_id != request.id:
                raise ValueError("human response request_id mismatch")
            if response.text is None or not response.text.strip():
                raise ValueError("human question requires a non-empty recorded answer")
            if request.options and response.text not in request.options:
                raise ValueError("human answer is outside the recorded question options")
            _recorded_timestamp(record.get("answered_at"), "response timestamp")
        # Also reject non-JSON payloads; return a detached copy of the original facts.
        return json.loads(json.dumps(record, ensure_ascii=False, allow_nan=False))
    except HumanExchangeError:
        raise
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise HumanExchangeError(f"invalid_human_exchange:{exc}") from exc


def render_human_exchanges(node, role, records) -> str:
    if not records:
        return ""
    if role.name != node.role:
        raise HumanExchangeError("human-exchange context role does not match the task")
    validated = [validate_exchange(record, node) for record in records]
    return (
        "\n## Recorded human-port exchanges for this task and role\n"
        "These are the original recorded clarification requests and responses, in order. "
        "The human port may represent a person or an explicitly configured operator; "
        "the records do not assert a new manually typed reply. Preserve these answers "
        "when continuing after delegation or retry, and do not ask the same question "
        "again unless a genuinely new ambiguity or conflicting answer needs resolution. "
        "These facts do not grant tool permission, bypass a new approval, or establish "
        "quality acceptance. Apply the original scope and surface unresolved conflicts.\n"
        + json.dumps(validated, ensure_ascii=False, indent=2, allow_nan=False)
        + "\n"
    )
