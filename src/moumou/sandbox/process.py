"""Bounded async subprocess execution with process-tree cleanup."""

import asyncio
import math
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path

from moumou.contracts import ToolResult
from moumou.sandbox.output import MAX_OUTPUT, clip_bytes


def timeout_value(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout_s must be a finite positive number")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout_s must be finite and positive")
    return value


async def run_command(command, cwd: Path, timeout=60.0, env=None):
    timeout = timeout_value(timeout)
    child_env = dict(os.environ)
    child_env.update(env or {})
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    # Existing P0 acceptance checks use python3. Keep that alias tied to the
    # active project environment without rewriting general shell expressions.
    if isinstance(command, str) and command.startswith("python3 "):
        exe = (
            subprocess.list2cmdline([sys.executable])
            if os.name == "nt"
            else shlex.quote(sys.executable)
        )
        command = exe + command[len("python3") :]
    kwargs = {
        "cwd": str(cwd),
        "env": child_env,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = None
    job = None
    readers = []

    async def drain(stream):
        kept = bytearray()
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return bytes(kept)
            remaining = MAX_OUTPUT + 1 - len(kept)
            if remaining > 0:
                kept.extend(chunk[:remaining])

    async def cleanup():
        if proc is None:
            return
        if os.name == "nt":
            if job:
                job.close()
            elif proc.returncode is None:
                proc.kill()
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await proc.wait()
        if readers:
            await asyncio.gather(*readers, return_exceptions=True)

    try:
        if isinstance(command, str):
            proc = await asyncio.create_subprocess_shell(command, **kwargs)
        else:
            proc = await asyncio.create_subprocess_exec(*command, **kwargs)
        if os.name == "nt" and proc.returncode is None:
            from moumou.sandbox.winjob import Job

            job = Job(proc.pid)
        readers = [asyncio.create_task(drain(proc.stdout)), asyncio.create_task(drain(proc.stderr))]

        async def collect():
            await proc.wait()
            return await asyncio.gather(*readers)

        out, err = await asyncio.wait_for(collect(), timeout)
        # Also terminate background descendants of a command that completed.
        await cleanup()
        stdout, t1 = clip_bytes(out)
        stderr, t2 = clip_bytes(err)
        code = proc.returncode
        return ToolResult(
            ok=code == 0,
            stdout=stdout,
            stderr=stderr,
            exit_code=code,
            error=None if code == 0 else f"exit code {code}",
            truncated=t1 or t2,
        )
    except TimeoutError:
        await cleanup()
        return ToolResult(ok=False, error=f"timeout after {timeout}s")
    except asyncio.CancelledError:
        await asyncio.shield(cleanup())
        raise
    except (OSError, ValueError) as exc:
        await cleanup()
        return ToolResult(ok=False, error=str(exc)[:1024])
    finally:
        if job:
            job.close()


async def run_process(cmd: str, cwd: Path, timeout: float) -> ToolResult:
    try:
        return await run_command(cmd, cwd, timeout)
    except (OSError, TypeError, ValueError) as exc:
        return ToolResult(ok=False, error=str(exc)[:1024])
