"""One-call snapshot of every process via NtQuerySystemInformation(SystemProcessInformation).

psutil issues a full system snapshot *per process* for thread counts / status on Windows, which is O(n^2) and takes
seconds under load. This reads everything we need for all processes in a single syscall.
x64 layout only.
"""
from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes
from dataclasses import dataclass

ntdll = ctypes.WinDLL("ntdll")
ntdll.NtQuerySystemInformation.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                           ctypes.POINTER(ctypes.c_ulong)]
ntdll.NtQuerySystemInformation.restype = ctypes.c_long
kernel32 = ctypes.WinDLL("kernel32")
kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]

SystemProcessInformation = 5
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004 - (1 << 32)
EPOCH_DIFF_100NS = 116444736000000000  # 1601-01-01 -> 1970-01-01
PROC_SIZE, THREAD_SIZE = 256, 80
assert ctypes.sizeof(ctypes.c_void_p) == 8, "procdeck needs 64-bit Python"

BASE_PRIORITY = {4: "idle", 6: "below_normal", 8: "normal", 10: "above_normal", 13: "high", 24: "realtime"}


@dataclass(slots=True)
class NtProc:
    pid: int
    ppid: int
    name: str
    ctime: float        # unix seconds (same base as psutil.Process.create_time())
    cpu_100ns: int      # user + kernel time
    threads: int
    session: int
    wset: int           # working set bytes
    private: int        # private bytes (commit)
    priority: str
    suspended: bool


_buf = ctypes.create_string_buffer(1 << 20)


def snapshot() -> list[NtProc]:
    global _buf
    ret = ctypes.c_ulong(0)
    while True:
        status = ntdll.NtQuerySystemInformation(SystemProcessInformation, _buf, len(_buf), ctypes.byref(ret))
        if status == STATUS_INFO_LENGTH_MISMATCH:
            _buf = ctypes.create_string_buffer(max(len(_buf) * 2, ret.value + (1 << 16)))
            continue
        if status != 0:
            raise OSError(f"NtQuerySystemInformation failed: 0x{status & 0xFFFFFFFF:08X}")
        break
    raw = _buf.raw[: ret.value] if ret.value else _buf.raw
    base_addr = ctypes.addressof(_buf)
    out: list[NtProc] = []
    off = 0
    while True:
        next_off, nthreads = struct.unpack_from("<II", raw, off)
        ctime_100ns, utime, ktime = struct.unpack_from("<qqq", raw, off + 32)
        name_len, _name_max, name_ptr = struct.unpack_from("<HH4xQ", raw, off + 56)
        base_pri, pid, ppid = struct.unpack_from("<i4xQQ", raw, off + 72)
        _handles, session = struct.unpack_from("<II", raw, off + 96)
        wset = struct.unpack_from("<Q", raw, off + 144)[0]
        private = struct.unpack_from("<Q", raw, off + 200)[0]
        if name_ptr and name_len:
            rel = name_ptr - base_addr
            name = raw[rel: rel + name_len].decode("utf-16-le", "replace") if 0 <= rel < len(raw) else "?"
        else:
            name = "System Idle Process" if pid == 0 else "?"
        # suspended = every thread waiting with WaitReason == Suspended (5)
        suspended = nthreads > 0
        toff = off + PROC_SIZE
        for _ in range(nthreads):
            state, reason = struct.unpack_from("<II", raw, toff + 68)
            if not (state == 5 and reason == 5):
                suspended = False
                break
            toff += THREAD_SIZE
        out.append(NtProc(
            pid=pid, ppid=ppid, name=name,
            ctime=(ctime_100ns - EPOCH_DIFF_100NS) / 1e7 if ctime_100ns else 0.0,
            cpu_100ns=utime + ktime, threads=nthreads, session=session, wset=wset, private=private,
            priority=BASE_PRIORITY.get(base_pri, str(base_pri)), suspended=suspended,
        ))
        if next_off == 0:
            break
        off += next_off
    return out


def current_session_id() -> int:
    import os
    s = wintypes.DWORD()
    kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(s))
    return s.value
