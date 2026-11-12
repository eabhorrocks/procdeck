"""Thin ctypes wrappers for the Windows APIs procdeck needs (no pywin32 dependency).

- hung (Not Responding) top-level windows
- system commit charge / cache (GetPerformanceInfo)
- job objects: CPU weight / hard cap, affinity, memory cap, terminate
"""
from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes

log = logging.getLogger("procdeck")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

# ---------------------------------------------------------------- hung windows
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsHungAppWindow.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]


def hung_window_pids() -> set[int]:
    """PIDs owning a visible top-level window that Windows considers hung ("Not Responding")."""
    pids: set[int] = set()

    def cb(hwnd, _lparam):
        try:
            if user32.IsWindowVisible(hwnd) and user32.IsHungAppWindow(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value:
                    pids.add(pid.value)
        except Exception:  # never let a callback exception escape into Windows
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return pids


# ---------------------------------------------------------------- performance info
class PERFORMANCE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("CommitTotal", ctypes.c_size_t), ("CommitLimit", ctypes.c_size_t), ("CommitPeak", ctypes.c_size_t),
        ("PhysicalTotal", ctypes.c_size_t), ("PhysicalAvailable", ctypes.c_size_t), ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t), ("KernelPaged", ctypes.c_size_t), ("KernelNonpaged", ctypes.c_size_t),
        ("PageSize", ctypes.c_size_t),
        ("HandleCount", wintypes.DWORD), ("ProcessCount", wintypes.DWORD), ("ThreadCount", wintypes.DWORD),
    ]


psapi.GetPerformanceInfo.argtypes = [ctypes.POINTER(PERFORMANCE_INFORMATION), wintypes.DWORD]


def performance_info() -> dict:
    """Commit charge and cache in bytes."""
    pi = PERFORMANCE_INFORMATION()
    pi.cb = ctypes.sizeof(pi)
    if not psapi.GetPerformanceInfo(ctypes.byref(pi), pi.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    ps = pi.PageSize
    return {
        "commit_total": pi.CommitTotal * ps,
        "commit_limit": pi.CommitLimit * ps,
        "system_cache": pi.SystemCache * ps,
        "process_count": pi.ProcessCount,
        "thread_count": pi.ThreadCount,
    }


# ---------------------------------------------------------------- job objects
JobObjectBasicLimitInformation = 2
JobObjectExtendedLimitInformation = 9
JobObjectCpuRateControlInformation = 15

JOB_OBJECT_LIMIT_AFFINITY = 0x00000010
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200

JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_WEIGHT_BASED = 0x2
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4

JOB_OBJECT_ALL_ACCESS = 0x1F001F
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
    _fields_ = [("ControlFlags", wintypes.DWORD), ("Value", wintypes.DWORD)]  # Value = CpuRate or Weight


kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenJobObjectW.restype = wintypes.HANDLE
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
kernel32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
                                               ctypes.POINTER(wintypes.DWORD)]
kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def _check(ok, what: str):
    if not ok:
        err = ctypes.get_last_error()
        raise OSError(err, f"{what} failed: {ctypes.FormatError(err).strip()}")
    return ok


class Job:
    """A Windows job object. Limits apply to every process in it, including children started later."""

    def __init__(self, handle: int, name: str | None):
        self.handle = handle
        self.name = name
        self._lock = threading.Lock()

    @classmethod
    def create(cls, name: str | None = None) -> "Job":
        h = kernel32.CreateJobObjectW(None, name)
        _check(h, "CreateJobObject")
        return cls(h, name)

    @classmethod
    def open(cls, name: str) -> "Job | None":
        h = kernel32.OpenJobObjectW(JOB_OBJECT_ALL_ACCESS, False, name)
        return cls(h, name) if h else None

    def close(self) -> None:
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = 0

    # -- membership
    def assign_current_process(self) -> None:
        _check(kernel32.AssignProcessToJobObject(self.handle, kernel32.GetCurrentProcess()), "AssignProcessToJobObject")

    def assign_pid(self, pid: int) -> None:
        hp = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        _check(hp, f"OpenProcess({pid})")
        try:
            _check(kernel32.AssignProcessToJobObject(self.handle, hp), f"AssignProcessToJobObject({pid})")
        finally:
            kernel32.CloseHandle(hp)

    def terminate(self, exit_code: int = 1) -> None:
        _check(kernel32.TerminateJobObject(self.handle, exit_code), "TerminateJobObject")

    # -- limits
    def _get_ext(self) -> JOBOBJECT_EXTENDED_LIMIT_INFORMATION:
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        _check(kernel32.QueryInformationJobObject(self.handle, JobObjectExtendedLimitInformation, ctypes.byref(info),
                                                  ctypes.sizeof(info), None), "QueryInformationJobObject")
        return info

    def _set_ext(self, info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION) -> None:
        _check(kernel32.SetInformationJobObject(self.handle, JobObjectExtendedLimitInformation, ctypes.byref(info),
                                                ctypes.sizeof(info)), "SetInformationJobObject(limits)")

    def set_affinity(self, cores: list[int] | None) -> None:
        """Restrict all processes in the job to these logical CPUs (None = no restriction)."""
        with self._lock:
            info = self._get_ext()
            b = info.BasicLimitInformation
            if cores:
                b.LimitFlags |= JOB_OBJECT_LIMIT_AFFINITY
                b.Affinity = sum(1 << c for c in cores)
            else:
                b.LimitFlags &= ~JOB_OBJECT_LIMIT_AFFINITY
                b.Affinity = 0
            self._set_ext(info)

    def set_memory_cap(self, gb: float | None) -> None:
        """Hard cap on total committed memory of the job (None = no cap)."""
        with self._lock:
            info = self._get_ext()
            b = info.BasicLimitInformation
            if gb:
                b.LimitFlags |= JOB_OBJECT_LIMIT_JOB_MEMORY
                info.JobMemoryLimit = int(gb * 1024 ** 3)
            else:
                b.LimitFlags &= ~JOB_OBJECT_LIMIT_JOB_MEMORY
                info.JobMemoryLimit = 0
            self._set_ext(info)

    def set_cpu(self, weight: int | None = None, cap_pct: float | None = None) -> None:
        """CPU share: weight 1-9 (relative), or hard cap in % of the whole machine, or neither (unlimited)."""
        info = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
        if weight is not None:
            info.ControlFlags = JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_WEIGHT_BASED
            info.Value = max(1, min(9, int(weight)))
        elif cap_pct is not None:
            info.ControlFlags = JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
            info.Value = max(1, min(10000, int(round(cap_pct * 100))))
        else:
            info.ControlFlags = 0
            info.Value = 0
        with self._lock:
            _check(kernel32.SetInformationJobObject(self.handle, JobObjectCpuRateControlInformation,
                                                    ctypes.byref(info), ctypes.sizeof(info)),
                   "SetInformationJobObject(cpu rate)")

    def get_cpu(self) -> dict:
        info = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
        _check(kernel32.QueryInformationJobObject(self.handle, JobObjectCpuRateControlInformation, ctypes.byref(info),
                                                  ctypes.sizeof(info), None), "QueryInformationJobObject(cpu)")
        f = info.ControlFlags
        if not f & JOB_OBJECT_CPU_RATE_CONTROL_ENABLE:
            return {"mode": "none", "value": None}
        if f & JOB_OBJECT_CPU_RATE_CONTROL_WEIGHT_BASED:
            return {"mode": "weight", "value": info.Value}
        return {"mode": "cap", "value": info.Value / 100}

    def apply_profile(self, prof: dict, cores: list[int] | None) -> list[str]:
        """Apply cores / cpu_weight / cpu_cap / mem_cap_gb. Returns a list of warnings (never raises)."""
        warns = []
        steps = [
            ("cores", lambda: self.set_affinity(cores) if cores else None),
            ("cpu", lambda: self.set_cpu(prof.get("cpu_weight"), prof.get("cpu_cap"))
                if ("cpu_weight" in prof or "cpu_cap" in prof) else None),
            ("mem_cap_gb", lambda: self.set_memory_cap(prof.get("mem_cap_gb")) if prof.get("mem_cap_gb") else None),
        ]
        for what, fn in steps:
            try:
                fn()
            except OSError as e:
                warns.append(f"{what}: {e}")
        return warns


# ---------------------------------------------------------------- AppData redirection
def appdata_redirected() -> bool:
    """True if this process's AppData writes are redirected into a packaged app's private folder.

    Happens for processes started from inside an MSIX-packaged app such as the Claude desktop app (even without a
    package identity of their own). Everything they start inherits it, so the daemon must not be started that way:
    its jobs' MATLAB prefs, caches etc. would silently land in the app's private copy of AppData.
    Detected empirically: write a probe file and see whether it shows up under Packages\\*\\LocalCache.
    """
    import os
    import uuid
    from pathlib import Path

    la = os.environ.get("LOCALAPPDATA")
    if not la:
        return False
    name = f"procdeck-probe-{uuid.uuid4().hex[:12]}"
    probe = Path(la) / name
    try:
        probe.write_text("x")
    except OSError:
        return False
    try:
        pk = Path(la) / "Packages"
        return pk.is_dir() and any((d / "LocalCache" / "Local" / name).exists() for d in pk.iterdir())
    except OSError:
        return False
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- notifications
def toast(title: str, msg: str) -> None:
    """Fire-and-forget Windows toast notification."""

    def _show():
        try:
            from winotify import Notification
            Notification(app_id="procdeck", title=title, msg=msg, duration="short").show()
        except Exception as e:  # notifications are best-effort
            log.debug("toast failed: %s", e)

    threading.Thread(target=_show, daemon=True).start()
