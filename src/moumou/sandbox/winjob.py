"""Windows Job Object: closing the handle terminates all associated processes."""

import ctypes
from ctypes import wintypes as w


class _Basic(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", w.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", w.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", w.DWORD),
        ("SchedulingClass", w.DWORD),
    ]


class _IO(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _Extended(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _Basic),
        ("IoInfo", _IO),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class Job:
    def __init__(self, pid):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.OpenProcess.restype = w.HANDLE
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = _Extended()
            info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not self.kernel.SetInformationJobObject(
                w.HANDLE(self.handle), 9, ctypes.byref(info), ctypes.sizeof(info)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            process = self.kernel.OpenProcess(0x0100 | 0x0001, False, pid)
            if not process:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not self.kernel.AssignProcessToJobObject(
                    w.HANDLE(self.handle), w.HANDLE(process)
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                self.kernel.CloseHandle(w.HANDLE(process))
        except BaseException:
            self.close()
            raise

    def resume(self, pid):
        """Resume the primary thread only after the suspended process owns a Job."""
        self.kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
        self.kernel.OpenThread.restype = w.HANDLE
        self.kernel.ResumeThread.restype = w.DWORD
        snapshot = self.kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if snapshot == w.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = _ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            valid = self.kernel.Thread32First(w.HANDLE(snapshot), ctypes.byref(entry))
            while valid:
                if entry.th32OwnerProcessID == pid:
                    thread = self.kernel.OpenThread(0x0002, False, entry.th32ThreadID)
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if self.kernel.ResumeThread(w.HANDLE(thread)) == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                        return
                    finally:
                        self.kernel.CloseHandle(w.HANDLE(thread))
                valid = self.kernel.Thread32Next(w.HANDLE(snapshot), ctypes.byref(entry))
            raise OSError("suspended process primary thread not found")
        finally:
            self.kernel.CloseHandle(w.HANDLE(snapshot))

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(w.HANDLE(self.handle))
            self.handle = None


class _ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD),
        ("cntUsage", w.DWORD),
        ("th32ThreadID", w.DWORD),
        ("th32OwnerProcessID", w.DWORD),
        ("tpBasePri", w.LONG),
        ("tpDeltaPri", w.LONG),
        ("dwFlags", w.DWORD),
    ]
