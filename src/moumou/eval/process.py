"""Parent-owned Windows worker containment with bounded observation waits.

The worker starts suspended and cannot execute before joining its Job. An outer
Job also contains P1 tools' nested Jobs. No result or log protocol uses pipes:
all three standard streams go to DEVNULL, so inherited pipe handles cannot hold
the parent open. The caller exchanges bounded, validated files instead.

Timeout covers launch, bootstrap and execution; cleanup has a separate grace.
The short synchronous OS launch/Job API calls themselves cannot be preempted by
an asyncio deadline. Unsupported platforms are rejected rather than receiving
a weaker process-group guarantee that misses P1 tools' separate sessions.
"""

from __future__ import annotations

import asyncio
import ctypes
import math
import subprocess
import sys
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path
from time import monotonic

from moumou.sandbox.winjob import Job

_POLL_S = 0.02
_CREATE_SUSPENDED = 0x00000004
_JOB_BASIC_ACCOUNTING = 1
_JOB_TERMINATION_CODE = 1


@dataclass
class ProcessOutcome:
    status: str
    pid: int | None
    exit_code: int | None
    elapsed_s: float
    cleanup_s: float
    cleanup_complete: bool
    error: str | None = None
    cancelled: bool = False

    def asdict(self) -> dict:
        return asdict(self)


class _Accounting(ctypes.Structure):
    # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION, class 1. LARGE_INTEGER is signed
    # 64-bit and DWORD is 32-bit on Windows (including 64-bit Windows).
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _JobControl:
    """Use P1's attachment/resume policy, retain the handle to verify termination."""

    def __init__(self, pid: int):
        # Resolve/signature-check APIs before constructing the owned Job; a
        # subsequent setup error must not lose a newly created Job handle.
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self.kernel.TerminateJobObject.restype = wintypes.BOOL
        self.kernel.QueryInformationJobObject.argtypes = (
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        self.kernel.QueryInformationJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.job = Job(pid)

    def resume(self, pid: int) -> None:
        self.job.resume(pid)

    def active_processes(self) -> int:
        info = _Accounting()
        if not self.kernel.QueryInformationJobObject(
            wintypes.HANDLE(self.job.handle), _JOB_BASIC_ACCOUNTING,
            ctypes.byref(info), ctypes.sizeof(info), None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.ActiveProcesses

    def terminate(self) -> None:
        if not self.kernel.TerminateJobObject(
            wintypes.HANDLE(self.job.handle), _JOB_TERMINATION_CODE,
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self.job.handle:
            if not self.kernel.CloseHandle(wintypes.HANDLE(self.job.handle)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.job.handle = None


def _positive_seconds(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite positive number")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{label} must be a finite positive number")
    return seconds


def _error(exc: BaseException) -> str:
    # Never include argv, child environment or a client/configuration object.
    return f"{type(exc).__name__}: {str(exc)[:500]}"


async def _cleanup(
    process: subprocess.Popen | None, job: _JobControl | None, timeout_s: float,
) -> tuple[bool, str | None]:
    """Confirm both worker exit and zero live Job members before closing it."""
    if process is None:
        return True, None
    deadline = monotonic() + timeout_s
    errors: list[str] = []
    accounting_valid = job is not None
    group_empty = job is None  # Attachment failed: root was never resumed.
    try:
        if job is not None:
            try:
                group_empty = job.active_processes() == 0
                if not group_empty:
                    job.terminate()
            except Exception as exc:
                accounting_valid = False
                errors.append(f"Job termination/accounting failed: {_error(exc)}")

        # Also terminate the known root if Job attachment or termination failed.
        # Only this Popen handle is used; never search or kill by executable name.
        if process.poll() is None:
            try:
                process.kill()
            except OSError as exc:
                if process.poll() is None:
                    errors.append(f"Worker termination failed: {_error(exc)}")

        while True:
            root_exited = process.poll() is not None
            if job is not None and accounting_valid:
                try:
                    group_empty = job.active_processes() == 0
                except Exception as exc:
                    accounting_valid = False
                    errors.append(f"Job accounting failed: {_error(exc)}")
            if root_exited and group_empty:
                break
            if root_exited and not accounting_valid:
                # A failed query cannot become positive evidence by waiting.
                # Close the owned Job as fallback and report failed cleanup.
                break
            remaining = deadline - monotonic()
            if remaining <= 0:
                if not root_exited:
                    errors.append("Worker did not exit within the cleanup deadline")
                if not group_empty:
                    errors.append("Zero live Job members could not be confirmed")
                break
            await asyncio.sleep(min(_POLL_S, remaining))
    finally:
        if job is not None:
            try:
                # KILL_ON_JOB_CLOSE is a final fallback, not evidence of a verified
                # empty Job when accounting/termination failed above.
                job.close()
            except Exception as exc:
                errors.append(f"Job handle release failed: {_error(exc)}")
    return not errors and process.poll() is not None and group_empty, "; ".join(errors) or None


def _observe_cleanup_exception(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()


async def _await_cleanup(
    task: asyncio.Task, timeout_s: float,
) -> tuple[tuple[bool, str | None], bool]:
    """Repeated cancellation cannot abandon an already bounded cleanup task."""
    interrupted = False
    deadline = monotonic() + timeout_s
    while True:
        try:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError
            return await asyncio.wait_for(asyncio.shield(task), remaining), interrupted
        except TimeoutError:
            task.cancel()
            task.add_done_callback(_observe_cleanup_exception)
            return (False, "Cleanup observation exceeded its deadline"), interrupted
        except asyncio.CancelledError:
            if task.cancelled():
                return (False, "Cleanup task was cancelled before confirmation"), True
            interrupted = True
        except Exception as exc:
            return (False, f"Cleanup failed: {_error(exc)}"), interrupted


async def supervise(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout_s: float,
    cleanup_timeout_s: float = 5.0, on_started: Callable[[int], None] | None = None,
) -> ProcessOutcome:
    """Execute one contained worker and return its verified lifecycle outcome.

    Cancellation is returned as ``cancelled=True`` after cleanup so that the
    higher-level runner can persist a cancelled result before re-raising it.
    ``cleanup_failed`` takes precedence as status if containment cannot be
    verified; callers must not continue the batch in that case.
    """
    if sys.platform != "win32":
        raise NotImplementedError("Hard evaluation workers currently require native Windows")
    timeout_s = _positive_seconds(timeout_s, "timeout_s")
    cleanup_timeout_s = _positive_seconds(cleanup_timeout_s, "cleanup_timeout_s")
    if not argv or not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise ValueError("worker argv must be a nonempty list of strings")
    started = monotonic()
    deadline = started + timeout_s
    process = None
    job = None
    status, error, cancelled = "exited", None, False
    try:
        process = subprocess.Popen(
            argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED
                | subprocess.CREATE_NO_WINDOW
            ),
            close_fds=True, shell=False,
        )
        job = _JobControl(process.pid)
        if on_started is not None:
            on_started(process.pid)
        if monotonic() >= deadline:
            status = "timeout"
        else:
            job.resume(process.pid)
            while True:
                # Deadline wins a race with late exit; an overdue worker cannot
                # publish a successful result merely because poll sees it exit.
                remaining = deadline - monotonic()
                if remaining <= 0:
                    status = "timeout"
                    break
                if process.poll() is not None:
                    break
                await asyncio.sleep(min(_POLL_S, remaining))
    except asyncio.CancelledError:
        status, cancelled = "cancelled", True
    except Exception as exc:
        status, error = "start_failed", _error(exc)
    finally:
        cleanup_started = monotonic()
        cleanup_task = asyncio.create_task(_cleanup(process, job, cleanup_timeout_s))
        (complete, cleanup_error), interrupted = await _await_cleanup(
            cleanup_task, cleanup_timeout_s,
        )
        if not complete:
            # An asynchronous cleanup fault must not prevent the final close/
            # kill request. These synchronous OS calls do not wait for exit.
            if job is not None:
                try:
                    job.close()
                except Exception:
                    pass  # The recorded cleanup error remains authoritative.
            if process is not None:
                try:
                    if process.poll() is None:
                        process.kill()
                except Exception:
                    pass
        cancelled = cancelled or interrupted
        cleanup_s = monotonic() - cleanup_started
    if not complete:
        status = "cleanup_failed"
        error = "; ".join(part for part in (error, cleanup_error) if part)
    elif cancelled:
        status = "cancelled"
    return ProcessOutcome(
        status=status, pid=process.pid if process is not None else None,
        exit_code=process.returncode if process is not None else None,
        elapsed_s=round(monotonic() - started, 6), cleanup_s=round(cleanup_s, 6),
        cleanup_complete=complete, error=error, cancelled=cancelled,
    )
