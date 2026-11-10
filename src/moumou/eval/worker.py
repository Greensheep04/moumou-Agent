"""Private child-process entry point for one supervised evaluation attempt.

Only standard-library imports live at module scope. The parent supplies a JSON
request without credentials and owns the final result; this process publishes a
nonce-bound candidate only after its client and Engine resources are closed.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import os
import re
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
API_KEY_ENV = "MOUMOU_EVAL_API_KEY"
_REFERENCE = re.compile(r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*):([A-Za-z_]\w*)\Z")


def _write_json(path: Path, data: dict) -> None:
    """Keep startup diagnostics available even if framework imports fail."""
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _sanitize(value: str, key: str) -> str:
    if key:
        value = value.replace(key, "[REDACTED]")
    value = re.sub(r"(?i)(Bearer\s+)[^\s\"'<>]+", r"\1[REDACTED]", value)
    return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", value)[:4000]


def _read_request(path: Path) -> dict:
    if path.stat().st_size > MAX_REQUEST_BYTES:
        raise ValueError("worker request is too large")
    request = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(request, dict) or request.get("schema_version") != 1:
        raise ValueError("unsupported worker request schema")
    if not isinstance(request.get("nonce"), str) or not re.fullmatch(
        r"[0-9a-f]{32}", request["nonce"]
    ):
        raise ValueError("invalid worker request nonce")
    if not isinstance(request.get("root"), str) or not Path(request["root"]).is_absolute():
        raise ValueError("worker root must be an absolute path")
    repeat = request.get("repeat")
    if not isinstance(repeat, int) or isinstance(repeat, bool) or repeat < 0:
        raise ValueError("invalid worker repeat")
    if not isinstance(request.get("eval_id"), str) or not request["eval_id"]:
        raise ValueError("invalid worker batch identity")
    if request.get("workspace_backend") not in ("local", "git"):
        raise ValueError("invalid worker workspace backend")
    if not isinstance(request.get("fixture"), dict) or not isinstance(
        request.get("client"), dict
    ):
        raise ValueError("invalid worker fixture or client specification")
    return request


def _build_client(spec: dict, fixture, key: str):
    from moumou.adapters import DeepSeekClient, FakeLLM
    from moumou.adapters.llm import LLMResponse

    kind = spec.get("kind")
    if kind == "deepseek":
        if "api_key" in spec:
            raise ValueError("worker request must not contain an API key")
        if not key:
            raise ValueError("worker API credential is missing")
        try:
            return DeepSeekClient(
                api_key=key, base_url=spec.get("base_url"), model=spec.get("model"),
                timeout_s=spec.get("timeout_s", 180.0),
                max_retries=spec.get("max_retries", 3),
            )
        finally:
            # Existing client initialization loads .env. The client retains its
            # credential; tools launched by the worker do not need to inherit it.
            os.environ.pop("DEEPSEEK_API_KEY", None)
    if kind == "fake":
        responses = spec.get("responses")
        if not isinstance(responses, list):
            raise ValueError("fake worker requires a response queue")
        return FakeLLM([LLMResponse.model_validate(item) for item in responses])
    if kind == "factory":
        reference = spec.get("reference")
        match = _REFERENCE.fullmatch(reference) if isinstance(reference, str) else None
        if match is None:
            raise ValueError("worker factory must be a trusted module:function reference")
        factory = getattr(importlib.import_module(match[1]), match[2])
        if not callable(factory):
            raise TypeError("worker factory is not callable")
        client = factory(fixture)
        if inspect.isawaitable(client):
            if inspect.iscoroutine(client):
                client.close()
            raise TypeError("worker factory must return a client synchronously")
        return client
    raise ValueError("unsupported worker client kind")


async def _close_client(client) -> None:
    close = getattr(client, "aclose", None)
    if callable(close):
        result = close()
        if inspect.isawaitable(result):
            await result


async def execute(request_path: Path) -> int:
    request_path = Path(request_path).resolve()
    directory = request_path.parent
    state_path = directory / "worker-state.json"
    raw_path = directory / "raw-result.json"
    candidate_path = directory / "candidate.json"
    error_path = directory / "worker-error.json"
    key = os.environ.pop(API_KEY_ENV, "")
    os.environ.pop("DEEPSEEK_API_KEY", None)
    stage = "request"
    request = None
    client = None
    closing = False

    def state(phase: str) -> None:
        _write_json(state_path, {
            "schema_version": 1, "nonce": request["nonce"], "pid": os.getpid(),
            "phase": phase, "at": datetime.now(UTC).isoformat(),
        })

    try:
        request = _read_request(request_path)
        state("starting")
        stage = "client"
        from moumou.eval.artifacts import _model_metadata
        from moumou.eval.loader import TaskFixture
        from moumou.eval.runner import run_once

        fixture = TaskFixture.model_validate(request["fixture"])
        try:
            client = _build_client(request["client"], fixture, key)
        finally:
            # A trusted factory can also construct a client whose initializer
            # loads .env; no tool needs to inherit either credential variable.
            loaded_key = os.environ.get(API_KEY_ENV) or os.environ.get("DEEPSEEK_API_KEY")
            if isinstance(loaded_key, str) and loaded_key:
                key = loaded_key
            os.environ.pop(API_KEY_ENV, None)
            os.environ.pop("DEEPSEEK_API_KEY", None)
        client_key = getattr(client, "api_key", None)
        if isinstance(client_key, str) and client_key:
            key = client_key
        if not callable(getattr(client, "chat", None)):
            raise TypeError("worker factory returned no LLM chat client")
        _write_json(directory / "worker-client.json", _model_metadata(client))
        stage = "execution"
        state("running")
        try:
            result = await run_once(
                fixture, client, Path(request["root"]), request["repeat"],
                workspace_backend=request["workspace_backend"], eval_id=request["eval_id"],
                _result_path=raw_path,
            )
            result_data = asdict(result)
        except Exception:
            # run_once records an ordinary setup/execution exception before
            # propagating it. That is a valid failed attempt, not a worker crash.
            if not raw_path.is_file() or raw_path.stat().st_size > MAX_RESULT_BYTES:
                raise
            result_data = json.loads(raw_path.read_text(encoding="utf-8"))
            if not isinstance(result_data, dict) or result_data.get("status") != "error":
                raise ValueError("worker did not persist a structured exception result") from None
        stage = "close"
        closing = True
        await _close_client(client)
        client = None
        stage = "publish"
        _write_json(candidate_path, {
            "schema_version": 1, "nonce": request["nonce"], "result": result_data,
        })
        state("finished")
        return 0
    except BaseException as exc:
        if client is not None and not closing:
            try:
                await _close_client(client)
            except BaseException:
                pass  # Parent deadline still bounds a blocked cleanup operation.
        try:
            _write_json(error_path, {
                "schema_version": 1,
                "nonce": request.get("nonce") if isinstance(request, dict) else None,
                "pid": os.getpid(), "stage": stage,
                "error_type": type(exc).__name__, "message": _sanitize(str(exc), key),
            })
            if isinstance(request, dict) and "nonce" in request:
                state("error")
        except (OSError, TypeError, ValueError):
            pass  # Exit status remains evidence when the artifact filesystem fails.
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Private supervised evaluation worker")
    parser.add_argument("--request", required=True)
    args = parser.parse_args(argv)
    return asyncio.run(execute(Path(args.request)))


if __name__ == "__main__":
    raise SystemExit(main())
