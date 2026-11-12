"""Steer running process trees: priority, cores, CPU share (job objects), memory cap, suspend/resume/kill."""
from __future__ import annotations

import fnmatch
import logging
import threading

import psutil

from . import win32
from .config import load_profiles, parse_cores, PRIORITIES

log = logging.getLogger("procdeck")

PRIORITY = {"idle": psutil.IDLE_PRIORITY_CLASS, "below_normal": psutil.BELOW_NORMAL_PRIORITY_CLASS,
            "normal": psutil.NORMAL_PRIORITY_CLASS, "above_normal": psutil.ABOVE_NORMAL_PRIORITY_CLASS,
            "high": psutil.HIGH_PRIORITY_CLASS}


class Controller:
    def __init__(self, sampler, runner):
        self.sampler = sampler
        self.runner = runner
        self.jobs: dict[tuple[int, int], win32.Job] = {}  # steer jobs we created, kept open for the daemon's life
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- helpers
    def _root_ctime(self, pid: int) -> float:
        r = self.sampler.rows.get(pid)
        if r:
            return r.ctime
        try:
            return psutil.Process(pid).create_time()
        except psutil.Error:
            raise ValueError(f"process {pid} not found")

    def _managed_job(self, pid: int) -> win32.Job | None:
        m = self.runner.managed.get(pid)
        if m and m.get("job_name"):
            return win32.Job.open(m["job_name"])
        return None

    def _steer_job(self, pid: int, pids: list[int], create: bool) -> tuple[win32.Job | None, list[str]]:
        key = (pid, int(self._root_ctime(pid)))
        warns: list[str] = []
        with self.lock:
            job = self.jobs.get(key)
            if job is None and create:
                job = win32.Job.create(f"Local\\procdeck-steer-{key[0]}-{key[1]}")
                self.jobs[key] = job
                for p in pids:
                    try:
                        job.assign_pid(p)
                    except OSError as e:
                        warns.append(f"pid {p}: {e.strerror if hasattr(e, 'strerror') else e}")
        return job, warns

    def tree(self, pid: int) -> list[int]:
        pids = self.sampler.tree_pids(pid)
        if not pids:
            raise ValueError(f"process {pid} not found")
        return pids

    # ---------------------------------------------------------------- queries
    def info(self, pid: int) -> dict:
        out = {"pid": pid, "cpu_share": {"mode": "none", "value": None}, "job": None}
        job = self._managed_job(pid)
        owned = False
        if job is None:
            try:
                key = (pid, int(self._root_ctime(pid)))
            except ValueError:
                return out
            job = self.jobs.get(key)
            owned = True
        else:
            out["job"] = "step"
        if job:
            try:
                out["cpu_share"] = job.get_cpu()
                out["job"] = out["job"] or "steer"
            except OSError:
                pass
            if not owned:
                job.close()
        return out

    # ---------------------------------------------------------------- actions
    def steer(self, pid: int, priority: str | None = None, cores=None, cpu_mode: str | None = None,
              cpu_value: float | None = None, mem_cap_gb: float | None = None, tree: bool = True) -> dict:
        """cpu_mode: 'weight' (value 1-9) | 'cap' (value %) | 'none'. mem_cap_gb: 0 removes the cap."""
        pids = self.tree(pid) if tree else [pid]
        applied, warns = [], []
        if priority:
            if priority not in PRIORITY:
                raise ValueError(f"priority must be one of {PRIORITIES[:-1] + ('high',)}")
            bad = 0
            for p in pids:
                try:
                    psutil.Process(p).nice(PRIORITY[priority])
                except psutil.Error:
                    bad += 1
            applied.append(f"priority {priority}")
            if bad:
                warns.append(f"priority: {bad}/{len(pids)} processes refused (access denied or exited)")
        job = self._managed_job(pid)
        managed = job is not None
        try:
            if cores is not None:
                cl = parse_cores(cores)
                if managed:
                    job.set_affinity(cl)
                else:
                    bad = 0
                    for p in pids:
                        try:
                            psutil.Process(p).cpu_affinity(cl)
                        except psutil.Error:
                            bad += 1
                    if bad:
                        warns.append(f"cores: {bad}/{len(pids)} processes refused")
                applied.append(f"cores {cores}")
            if cpu_mode or mem_cap_gb is not None:
                if not managed:
                    need = cpu_mode in ("weight", "cap") or bool(mem_cap_gb)
                    job, w = self._steer_job(pid, pids, create=need)
                    warns += w
                if job:
                    if cpu_mode == "weight":
                        job.set_cpu(weight=int(cpu_value or 5))
                        applied.append(f"cpu weight {int(cpu_value or 5)}")
                    elif cpu_mode == "cap":
                        job.set_cpu(cap_pct=float(cpu_value or 50))
                        applied.append(f"cpu cap {float(cpu_value or 50):.0f}%")
                    elif cpu_mode == "none":
                        job.set_cpu()
                        applied.append("cpu share unlimited")
                    if mem_cap_gb is not None:
                        job.set_memory_cap(mem_cap_gb or None)
                        applied.append(f"memory cap {mem_cap_gb} GB" if mem_cap_gb else "memory cap removed")
        except OSError as e:
            warns.append(str(e))
        finally:
            if managed and job:
                job.close()
        log.info("steer %s: %s %s", pid, applied, warns)
        return {"pid": pid, "processes": len(pids), "applied": applied, "warnings": warns}

    def apply_profile(self, pid: int, profile: str, tree: bool = True) -> dict:
        prof = load_profiles().get(profile)
        if prof is None:
            raise ValueError(f"unknown profile {profile!r}")
        mode, val = ("weight", prof["cpu_weight"]) if "cpu_weight" in prof else (
            ("cap", prof["cpu_cap"]) if "cpu_cap" in prof else (None, None))
        return self.steer(pid, priority=prof.get("priority"), cores=prof.get("cores"), cpu_mode=mode,
                          cpu_value=val, mem_cap_gb=prof.get("mem_cap_gb"), tree=tree)

    def signal(self, pid: int, action: str) -> dict:
        """suspend | resume | kill the whole tree."""
        if action == "kill" and pid in self.runner.managed:
            m = self.runner.managed[pid]
            self.runner.cancel(m["run_id"], m["step"])
            return {"pid": pid, "applied": ["cancelled step"], "warnings": []}
        pids = self.tree(pid)
        bad = 0
        order = pids if action != "kill" else list(reversed(pids))
        for p in order:
            try:
                proc = psutil.Process(p)
                {"suspend": proc.suspend, "resume": proc.resume, "kill": proc.kill}[action]()
            except psutil.Error:
                bad += 1
        warns = [f"{bad}/{len(pids)} processes refused or already exited"] if bad else []
        return {"pid": pid, "processes": len(pids), "applied": [action], "warnings": warns}


class RuleEngine:
    """Apply rules.yaml to newly seen processes (never to procdeck-managed step trees)."""

    def __init__(self, controller: Controller, rules: list[dict]):
        self.ctl = controller
        self.rules = rules
        self.seen: set[tuple[int, float]] = set()
        self.log: list[dict] = []

    def update_rules(self, rules: list[dict]) -> None:
        self.rules = rules
        self.seen.clear()  # re-evaluate everything against the new rules

    @staticmethod
    def matches(rule: dict, name: str, cmd: str, cwd: str | None) -> bool:
        m = rule.get("match") or {}
        if "exe" in m and not fnmatch.fnmatch(name.lower(), str(m["exe"]).lower()):
            return False
        if "cmd" in m and not fnmatch.fnmatch(cmd.lower(), str(m["cmd"]).lower()):
            return False
        if "cwd" in m:
            if not cwd:
                return False
            norm = lambda s: s.replace("\\", "/").lower().rstrip("/")  # noqa: E731
            if not fnmatch.fnmatch(norm(cwd), norm(str(m["cwd"]))):
                return False
        return True

    def _in_managed_tree(self, rows, pid: int) -> bool:
        seen = 0
        r = rows.get(pid)
        while r is not None and seen < 64:
            if r.pid in self.ctl.runner.managed:
                return True
            par = rows.get(r.ppid)
            if par is None or par.ctime > r.ctime + 0.5:
                return False
            r, seen = par, seen + 1
        return False

    def tick(self) -> None:
        rows = self.ctl.sampler.rows
        live = set()
        for r in rows.values():
            k = (r.pid, r.ctime)
            live.add(k)
            if k in self.seen or not r.own:
                continue
            self.seen.add(k)
            if not self.rules or self._in_managed_tree(rows, r.pid):
                continue
            for rule in self.rules:
                if self.matches(rule, r.name, " ".join(r.cmdline), r.cwd):
                    s = rule.get("set") or {}
                    try:
                        if "profile" in s:
                            res = self.ctl.apply_profile(r.pid, s["profile"], tree=False)
                        else:
                            mode, val = (("weight", s["cpu_weight"]) if "cpu_weight" in s else
                                         (("cap", s["cpu_cap"]) if "cpu_cap" in s else (None, None)))
                            res = self.ctl.steer(r.pid, priority=s.get("priority"), cores=s.get("cores"),
                                                 cpu_mode=mode, cpu_value=val, tree=False)
                        self.log.append({"pid": r.pid, "name": r.name, "label": r.label, "rule": rule, **res})
                        self.log = self.log[-100:]
                    except (ValueError, OSError) as e:
                        log.warning("rule on %s failed: %s", r.pid, e)
        self.seen &= live
