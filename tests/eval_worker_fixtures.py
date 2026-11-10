"""Importable offline probes for real supervised evaluation processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from moumou.adapters.llm import LLMResponse, ToolCall


def _write(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(path)


class ProbeClient:
    def __init__(self, fixture):
        self.mode = fixture.goal
        self.calls = 0
        self.request_path = Path(sys.argv[sys.argv.index("--request") + 1])
        self.request = json.loads(self.request_path.read_text(encoding="utf-8"))
        self.run_dir = Path(self.request["root"]) / fixture.id / f"r{self.request['repeat']}"
        if self.mode == "factory_env_probe":
            self.api_key = "sk-offline-factory-environment-placeholder"
            os.environ["DEEPSEEK_API_KEY"] = self.api_key

    def marker(self) -> None:
        _write(self.run_dir / "probe-pids.json", {"worker": os.getpid()})

    def children(self) -> None:
        marker = self.run_dir / "descendants.json"
        child = (
            "import json,os,subprocess,sys,time\n"
            "from pathlib import Path\n"
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
            f"path=Path({str(marker)!r})\n"
            "temp=path.with_suffix('.tmp')\n"
            "temp.write_text(json.dumps({'child':os.getpid(),'grandchild':child.pid}))\n"
            "temp.replace(path)\n"
            "time.sleep(60)\n"
        )
        subprocess.Popen(
            [sys.executable, "-c", child], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        deadline = time.monotonic() + 5
        while not marker.exists():
            if time.monotonic() > deadline:
                raise RuntimeError("offline descendants did not announce their PIDs")
            time.sleep(0.01)

    async def chat(self, messages, tools=None):
        self.calls += 1
        self.marker()
        if self.mode == "factory_env_probe":
            assert "MOUMOU_EVAL_API_KEY" not in os.environ
            assert "DEEPSEEK_API_KEY" not in os.environ
        if self.mode == "busy":
            while True:
                pass
        if self.mode == "sleep":
            time.sleep(60)
        if self.mode in ("descendants", "exit_after_child"):
            self.children()
            if self.mode == "exit_after_child":
                os._exit(17)
            time.sleep(60)
        if self.mode == "crash":
            os._exit(23)
        if self.mode == "trace_then_block":
            if self.calls == 1:
                return LLMResponse(tool_calls=[ToolCall(
                    id="write-marker", name="write_file",
                    args={"path": "trace-marker.txt", "content": "preserve before timeout"},
                )])
            time.sleep(60)
        if self.mode == "nested_tool":
            marker = self.run_dir / "nested-pids.json"
            script = (
                "import json,os,subprocess,sys,time\n"
                "from pathlib import Path\n"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
                f"path=Path({str(marker)!r})\n"
                "temp=path.with_suffix('.tmp')\n"
                "temp.write_text(json.dumps({'tool':os.getpid(),'grandchild':child.pid}))\n"
                "temp.replace(path)\n"
                "time.sleep(60)\n"
            )
            return LLMResponse(tool_calls=[ToolCall(
                id="nested", name="run_python", args={"code": script, "timeout_s": 60},
            )])
        return LLMResponse(content="The seeded answer is ready", tokens_in=7, tokens_out=3)

    async def aclose(self):
        if not self.mode.startswith("candidate_"):
            return
        directory = self.request_path.parent
        result = json.loads((directory / "raw-result.json").read_text(encoding="utf-8"))
        envelope = {"schema_version": 1, "nonce": self.request["nonce"], "result": result}
        if self.mode == "candidate_nonce":
            envelope["nonce"] = "0" * 32
        elif self.mode == "candidate_identity":
            result["fixture_id"] = "a-different-fixture"
        elif self.mode == "candidate_count_string":
            result["done"] = "1"
        elif self.mode == "candidate_wrong_target":
            result["root_acceptance_checks"][0]["target"] = "a-different-answer.txt"
        elif self.mode == "candidate_wrong_root":
            result["root_task_id"] = "a-root-that-does-not-exist"
        elif self.mode == "candidate_nan":
            result["completion_rate"] = float("nan")
        elif self.mode == "candidate_status":
            result["status"] = "made-up-success"
        elif self.mode == "candidate_oversize":
            result["errors"] = ["x" * (9 * 1024 * 1024)]
        path = directory / "candidate.json"
        if self.mode == "candidate_deep":
            path.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")
        elif self.mode == "candidate_truncated":
            path.write_text('{"schema_version":', encoding="utf-8")
        else:
            path.write_text(json.dumps(envelope), encoding="utf-8")
        if self.mode == "candidate_then_hang":
            time.sleep(60)
        if self.mode == "candidate_then_crash":
            os._exit(7)
        os._exit(0)  # Keep the injected candidate from being replaced by the worker entry point.


def make_client(fixture):
    return ProbeClient(fixture)
