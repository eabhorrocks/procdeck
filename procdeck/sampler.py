"""Process sampling, grouping into jobs/apps, and dead-process flags."""
from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass, field

import psutil

from . import labels, ntsnap, win32
from .config import format_cores

NCPU = psutil.cpu_count() or 1
GB = 1024 ** 3

# Processes that run in the user's session but belong to the system.
SYSTEM_IN_SESSION = {"csrss.exe", "winlogon.exe", "dwm.exe", "fontdrvhost.exe", "system", "registry",
                     "smss.exe", "lsass.exe", "services.exe", "wininit.exe", "memory compression"}


@dataclass
class Row:
    pid: int
    ppid: int
    name: str
    ctime: float
    cpu: float = 0.0          # % of the whole machine
    rss: int = 0
    private: int = 0
    threads: int = 0
    suspended: bool = False
    priority: str = "normal"
    own: bool = False
    cmdline: list[str] = field(default_factory=list)
    cwd: str | None = None
    kind: str = "app"
    label: str = ""
    script_key: str = ""
    flags: list[str] = field(default_factory=list)


def valid_parent(rows: dict[int, Row], r: Row) -> Row | None:
    """The parent row, unless the parent is gone or its PID was reused by a younger process."""
    p = rows.get(r.ppid)
    if p is None or p.pid == r.pid or p.ctime > r.ctime + 0.5:
        return None
    return p


def is_orphan(rows: dict[int, Row], r: Row, watch: list[str]) -> bool:
    # The daemon and step launchers are started detached, so their parent exiting is normal, not a leftover.
    if r.kind in ("daemon", "launcher"):
        return False
    return r.own and r.name.lower() in watch and r.ppid != 0 and valid_parent(rows, r) is None


def assign_groups(rows: dict[int, Row], managed_roots, compute: list[str], self_pid: int | None = None) -> dict:
    """Map pid -> group key.

    ('managed', pid)  procdeck step tree (rooted at its launcher)
    ('job', pid)      tree of compute processes (python/matlab/...) and everything they spawned
    ('app', name)     other processes of the current user, grouped by program
    ('system',)       other users' processes
    """
    memo: dict[int, tuple] = {}

    def group_of(pid: int, depth: int = 0) -> tuple:
        if pid in memo:
            return memo[pid]
        r = rows[pid]
        g = None
        if pid in managed_roots:
            g = ("managed", pid)
        elif pid == self_pid:
            g = ("job", pid)
        else:
            parent = valid_parent(rows, r) if depth < 64 else None
            if parent is not None and parent.pid != self_pid:
                pg = group_of(parent.pid, depth + 1)
                if pg[0] in ("managed", "job"):
                    g = pg
            if g is None:
                if not r.own:
                    g = ("system",)
                elif r.name.lower() in compute:
                    g = ("job", pid)
                else:
                    g = ("app", r.name.lower())
        memo[pid] = g
        return g

    for pid in rows:
        group_of(pid)
    return memo


class Sampler:
    def __init__(self, settings: dict):
        self.settings = settings
        self.self_pid = os.getpid()
        self.session = ntsnap.current_session_id()
        self.static: dict[tuple[int, float], dict] = {}   # (pid, ctime) -> cached cmdline/cwd/label
        self.prev_cpu: dict[tuple[int, float], int] = {}
        self.prev_t: float | None = None
        self.group_hist: dict[str, deque] = {}
        self.idle_since: dict[str, float] = {}
        self.aff_cache: dict[tuple[int, float], tuple[float, list[int]]] = {}
        self.hung: set[int] = set()
        self._hung_t = 0.0
        self.sys_hist: deque = deque(maxlen=600)
        self.rows: dict[int, Row] = {}
        self.tick_ms = 0.0
        psutil.cpu_percent(percpu=True)

    def update_settings(self, settings: dict) -> None:
        self.settings = settings

    # ------------------------------------------------------------------ helpers
    def _static(self, pid: int, ctime: float, name: str, own: bool, want_cmdline: bool) -> dict:
        key = (pid, ctime)
        s = self.static.get(key)
        if s is not None:
            return s
        s = {"cmdline": [], "cwd": None}
        if own and want_cmdline:
            try:
                p = psutil.Process(pid)
                s["cmdline"] = p.cmdline()
                s["cwd"] = p.cwd()
            except (psutil.Error, OSError):
                pass
        s["kind"], s["label"], s["script_key"] = labels.describe(name, s["cmdline"], s["cwd"])
        if pid == self.self_pid:
            s["kind"], s["label"] = "daemon", "procdeck daemon"
        elif "procdeck.launcher" in " ".join(s["cmdline"]):
            s["kind"], s["label"] = "launcher", "procdeck launcher"
        self.static[key] = s
        return s

    def _affinity(self, r: Row) -> list[int]:
        key = (r.pid, r.ctime)
        now = time.time()
        c = self.aff_cache.get(key)
        if c and now - c[0] < 5:
            return c[1]
        aff: list[int] = []
        try:
            aff = psutil.Process(r.pid).cpu_affinity()
        except (psutil.Error, OSError):
            pass
        self.aff_cache[key] = (now, aff)
        return aff

    # ------------------------------------------------------------------ main
    def tick(self, managed: dict[int, dict] | None = None) -> dict:
        """Sample everything. `managed` maps launcher pid -> {"run": name, "step": key, "run_id": id, ...}."""
        t0 = time.perf_counter()
        managed = managed or {}
        now = time.time()
        fs = self.settings["flags"]
        compute = self.settings["compute_exes"]

        if now - self._hung_t >= 2:
            try:
                self.hung = win32.hung_window_pids()
            except OSError:
                self.hung = set()
            self._hung_t = now

        snap = ntsnap.snapshot()
        warm = self.prev_t is not None  # CPU % needs a previous sample
        dt = (now - self.prev_t) if self.prev_t else None
        cur_cpu: dict[tuple[int, float], int] = {}
        rows: dict[int, Row] = {}
        for p in snap:
            if p.pid == 0:
                continue
            key = (p.pid, p.ctime)
            cur_cpu[key] = p.cpu_100ns
            prev = self.prev_cpu.get(key)
            cpu = 0.0
            if dt and prev is not None:
                cpu = max(0.0, (p.cpu_100ns - prev) / (dt * 1e7 * NCPU) * 100)
            lname = p.name.lower()
            own = p.session == self.session and lname not in SYSTEM_IN_SESSION and p.pid > 4
            s = self._static(p.pid, p.ctime, p.name, own, lname in compute)
            rows[p.pid] = Row(
                pid=p.pid, ppid=p.ppid, name=p.name, ctime=p.ctime, cpu=cpu, rss=p.wset, private=p.private,
                threads=p.threads, suspended=p.suspended, priority=p.priority, own=own,
                cmdline=s["cmdline"], cwd=s["cwd"], kind=s["kind"], label=s["label"], script_key=s["script_key"],
            )
        self.prev_cpu, self.prev_t = cur_cpu, now
        live = set(cur_cpu)
        for k in [k for k in self.static if k not in live]:
            del self.static[k]
        for k in [k for k in self.aff_cache if k not in live]:
            del self.aff_cache[k]

        # per-process flags
        for r in rows.values():
            if r.pid in self.hung:
                r.flags.append("hung")
            if r.suspended and (r.name.lower() in compute or r.pid in managed):
                r.flags.append("suspended")
            if is_orphan(rows, r, fs["orphan_watch"]) and r.pid not in managed:
                r.flags.append("orphan")

        gmap = assign_groups(rows, managed, compute, self.self_pid)
        members: dict[tuple, list[Row]] = {}
        for pid, g in gmap.items():
            members.setdefault(g, []).append(rows[pid])
        groups = [self._make_group(g, rs, rows, managed) for g, rs in members.items()]
        groups.sort(key=lambda x: x["rss"], reverse=True)

        # cpu history and idle-hog tracking
        seen = set()
        for gr in groups:
            gid = gr["id"]
            seen.add(gid)
            h = self.group_hist.setdefault(gid, deque([0.0] * 60, maxlen=60))
            h.append(round(gr["cpu"], 2))
            gr["cpu_hist"] = list(h)
            if gr["kind"] in ("job", "managed") and gr["kind_root"] != "daemon":
                if gr["cpu"] < fs["idle_cpu_below"] and gr["rss"] >= fs["idle_ram_gb"] * GB:
                    since = self.idle_since.setdefault(gid, now)
                    if now - since >= fs["idle_minutes"] * 60:
                        gr["flags"].append("idle")
                        gr["idle_for"] = now - since
                else:
                    self.idle_since.pop(gid, None)
        for gid in [g for g in self.group_hist if g not in seen]:
            del self.group_hist[gid]
            self.idle_since.pop(gid, None)

        system = self._system(len(rows), sum(r.threads for r in rows.values()))
        if warm:
            self.sys_hist.append((now, system["cpu"], system["ram_pct"]))
        self.rows = rows
        self.tick_ms = (time.perf_counter() - t0) * 1000
        daemon = rows.get(self.self_pid)
        return {"t": now, "system": system, "groups": groups, "warm": warm,
                "daemon_cpu": daemon.cpu if daemon else 0.0, "tick_ms": self.tick_ms}

    def _make_group(self, g: tuple, rs: list[Row], rows: dict[int, Row], managed: dict) -> dict:
        kind = g[0]
        root = rows[g[1]] if kind in ("managed", "job") else min(rs, key=lambda r: r.ctime)
        others = sorted((r for r in rs if r is not root), key=lambda r: r.rss, reverse=True)
        cwd = root.cwd
        if kind == "managed":
            m = managed[g[1]]
            gid = f"m:{root.pid}:{root.ctime:.0f}"
            label = f"{m['run']} › {m['step']}"
            child = next((r for r in others if r.ppid == root.pid), None)
            sub = " ".join(child.cmdline) if child and child.cmdline else m.get("cmd", "")
            cwd = (child.cwd if child else None) or m.get("cwd")
        elif kind == "job":
            gid = f"j:{root.pid}:{root.ctime:.0f}"
            label, sub = root.label, " ".join(root.cmdline) or root.name
        elif kind == "app":
            gid = f"a:{g[1]}"
            label = labels.friendly_name(root.name)
            sub = f"{root.name} ×{len(rs)}" if len(rs) > 1 else root.name
        else:
            gid = "sys"
            label, sub = "System & services", f"{len(rs)} processes (other users / services)"
        flags = sorted({f for r in rs for f in r.flags})
        aff = self._affinity(root) if kind in ("managed", "job") else []
        if kind == "system":
            others = others[:60]
        return {
            "id": gid, "kind": kind, "kind_root": root.kind, "label": label, "sub": sub, "cwd": cwd,
            "pid": root.pid, "ctime": root.ctime, "started": root.ctime,
            "cpu": sum(r.cpu for r in rs), "rss": sum(r.rss for r in rs), "private": sum(r.private for r in rs),
            "threads": sum(r.threads for r in rs), "nproc": len(rs),
            "priority": root.priority if kind != "system" else "-",
            "cores": format_cores(aff, NCPU) if aff else ("all" if kind == "app" else "-"), "affinity": aff,
            "flags": flags, "managed": managed.get(root.pid) if kind == "managed" else None,
            "script_key": root.script_key if kind == "job" else "",
            "children": [{"pid": r.pid, "name": r.name,
                          "label": r.label if r.kind not in ("app",) else labels.friendly_name(r.name),
                          "cpu": r.cpu, "rss": r.rss, "threads": r.threads, "flags": r.flags,
                          "priority": r.priority, "cmd": " ".join(r.cmdline)[:300]} for r in others],
        }

    def _system(self, nproc: int, nthreads: int) -> dict:
        per = psutil.cpu_percent(percpu=True)
        vm = psutil.virtual_memory()
        try:
            perf = win32.performance_info()
        except OSError:
            perf = {"commit_total": 0, "commit_limit": 1, "system_cache": 0}
        used = vm.total - vm.available
        return {
            "cpu": sum(per) / max(1, len(per)), "percpu": per, "ncpu": NCPU,
            "ram_total": vm.total, "ram_avail": vm.available, "ram_used": used, "ram_pct": used / vm.total * 100,
            "cache": perf["system_cache"], "commit_total": perf["commit_total"], "commit_limit": perf["commit_limit"],
            "commit_pct": perf["commit_total"] / max(1, perf["commit_limit"]) * 100,
            "nproc": nproc, "nthreads": nthreads,
        }

    def tree_pids(self, root_pid: int, stop: set[int] | frozenset = frozenset()) -> list[int]:
        """root + all descendants, from the last sample (falls back to psutil).

        Descendants in `stop` are left out together with their own subtrees (the root never is)."""
        rows = self.rows
        kids: dict[int, list[int]] = {}
        if root_pid in rows:
            for r in rows.values():
                par = valid_parent(rows, r)
                if par is not None:
                    kids.setdefault(par.pid, []).append(r.pid)
        else:
            try:
                for c in psutil.Process(root_pid).children(recursive=True):
                    try:
                        kids.setdefault(c.ppid(), []).append(c.pid)
                    except psutil.Error:
                        pass
            except psutil.Error:
                return []
        out, stack = [], [root_pid]
        while stack:
            pid = stack.pop()
            if pid != root_pid and pid in stop:
                continue
            out.append(pid)
            stack.extend(kids.get(pid, []))
        return out

    def tree_rss(self, root_pid: int) -> int:
        return sum(self.rows[p].rss for p in self.tree_pids(root_pid) if p in self.rows)
