"""Scheduler: admission control, launching, monitoring, retries."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psutil

from . import launch, replan, win32
from .config import LOG_DIR, REPO_DIR, format_cores, parse_cores
from .labels import script_key_from_cmd
from .spec import Plan

log = logging.getLogger("procdeck")
GB = 1024 ** 3
NCPU = psutil.cpu_count() or 1
DEFAULT_DURATION_S = 600
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000
FINAL = ("done", "failed", "cancelled", "skipped")


# ------------------------------------------------------------------------------------------- pure helpers
def admit(c: dict, ctx: dict) -> tuple[bool, str]:
    """Can candidate step `c` start now? Returns (ok, reason-if-not)."""
    if c.get("run_max_parallel") and ctx["run_running"].get(c["run_id"], 0) >= c["run_max_parallel"]:
        return False, f"run max_parallel ({c['run_max_parallel']}) reached"
    sk = (c["run_id"], c["name"])
    if c.get("step_max_parallel") and ctx["step_running"].get(sk, 0) >= c["step_max_parallel"]:
        return False, f"step max_parallel ({c['step_max_parallel']}) reached"
    if ctx.get("max_running") and ctx["running"] >= ctx["max_running"]:
        return False, f"max_running ({ctx['max_running']}) reached"
    if ctx["running"] > 0 and ctx["threads_running"] + c["threads"] > ctx["threads_budget"]:
        return False, f"CPU budget: {ctx['threads_running']}+{c['threads']} > {ctx['threads_budget']} threads"
    free = ctx["avail_gb"] - ctx["headroom_gb"] - ctx["outstanding_gb"]
    need = c["mem_gb"]
    never_fits = need > ctx["total_gb"] - ctx["headroom_gb"]
    if need > free and not (never_fits and ctx["running"] == 0):
        return False, f"needs {need:.1f} GB RAM, {max(0.0, free):.1f} GB free"
    return True, ""


def deps_state(step: dict, by_key: dict[str, dict]) -> str:
    """'ready' | 'wait' | 'blocked' (a dependency failed with on_fail: stop)."""
    st = "ready"
    for n in step["needs"]:
        d = by_key.get(n)
        if d is None:
            continue
        if d["state"] == "done":
            continue
        if d["state"] in ("failed", "skipped", "cancelled"):
            if d["state"] == "failed" and d["on_fail"] == "continue":
                continue
            return "blocked"
        st = "wait"
    return st


def simulate(steps: list[dict], now: float, ctx: dict, durations: dict[str, float]) -> dict[str, tuple[float, float]]:
    """Rough projected (start, end) per step key, honouring deps, max_parallel, threads and RAM.

    `steps`: dicts with key, name, run_id, needs, threads, mem_gb, state, started_at, start_at,
    run_max_parallel, step_max_parallel. Running steps end at started_at + duration.
    """
    dur = lambda s: durations.get(s["key"], DEFAULT_DURATION_S)  # noqa: E731
    proj: dict[str, tuple[float, float]] = {}
    active: list[dict] = []      # {"s": step, "end": t, "sim": bool}
    finished: set[str] = set()
    for s in steps:
        if s["state"] in FINAL:
            proj[s["key"]] = (s.get("started_at") or now, s.get("ended_at") or now)
            finished.add(s["key"])
        elif s["state"] == "running":
            end = max(now + 30, (s.get("started_at") or now) + dur(s))
            proj[s["key"]] = (s.get("started_at") or now, end)
            active.append({"s": s, "end": end, "sim": False})
    pending = sorted((s for s in steps if s["key"] not in proj), key=lambda s: s.get("seq", 0))
    t, sim_mem = now, 0.0
    relaxed = False  # True: assume today's RAM pressure from other processes has cleared
    for _guard in range(10000):
        if not pending:
            break
        for a in [a for a in active if a["end"] <= t]:
            active.remove(a)
            finished.add(a["s"]["key"])
            if a["sim"]:
                sim_mem -= a["s"]["mem_gb"]
        for s in list(pending):
            if any(n not in finished for n in s["needs"]) or (s.get("start_at") and s["start_at"] > t):
                continue
            run_running: dict = {}
            step_running: dict = {}
            for a in active:
                r = a["s"]
                run_running[r["run_id"]] = run_running.get(r["run_id"], 0) + 1
                step_running[(r["run_id"], r["name"])] = step_running.get((r["run_id"], r["name"]), 0) + 1
            sctx = {**ctx, "running": len(active), "threads_running": sum(a["s"]["threads"] for a in active),
                    "avail_gb": (ctx["total_gb"] if relaxed else ctx["avail_gb"]) - sim_mem, "outstanding_gb": 0.0,
                    "run_running": run_running, "step_running": step_running}
            if admit(s, sctx)[0]:
                proj[s["key"]] = (t, t + dur(s))
                active.append({"s": s, "end": t + dur(s), "sim": True})
                sim_mem += s["mem_gb"]
                pending.remove(s)
        if not pending:
            break
        nxt = [a["end"] for a in active if a["end"] > t]
        nxt += [s["start_at"] for s in pending if s.get("start_at") and s["start_at"] > t]
        if not nxt and not relaxed:
            # Nothing running and nothing admitted: free RAM is short right now (other processes). That is
            # temporary, so lay the rest out as if the whole machine were free rather than serialising it.
            relaxed = True
            continue
        if not nxt:  # nothing can ever be placed (e.g. never fits in RAM): queue the rest at the end
            for s in pending:
                proj[s["key"]] = (t, t + dur(s))
                t += dur(s)
            break
        t = min(nxt)
    return proj


def safe_name(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.\-\[\]]", "_", key).replace("[", "(").replace("]", ")")


# ------------------------------------------------------------------------------------------- runner
class Runner:
    def __init__(self, store, settings: dict, profiles: dict, sampler, notify=None):
        self.store = store
        self.settings = settings
        self.profiles = profiles
        self.sampler = sampler
        self.notify = notify or (lambda t, m: None)
        self.manual_pause = False
        self.alert_pause = False
        self.cancel_requested: set[tuple[str, str]] = set()
        self.alert_suspended: set[tuple[str, str]] = set()
        self.cpu_busy_t = time.time()
        self.managed: dict[int, dict] = {}
        self.admission_info: dict = {}
        self._refresh_managed()

    def update_config(self, settings: dict, profiles: dict) -> None:
        self.settings, self.profiles = settings, profiles

    # --------------------------------------------------------------- submission
    def enrich(self, plan: Plan) -> list[dict]:
        """Fill per-step threads, mem estimate (from history) and script key."""
        a = self.settings["admission"]
        out = []
        for s in plan.steps:
            s = dict(s)
            prof = self.profiles.get(s["profile"], {})
            s["threads"] = launch.step_threads(s, prof, NCPU)
            s["script_key"] = script_key_from_cmd(s["cmd"], s["cwd"])
            est = self.store.estimate(s["script_key"])
            s["history"] = est
            if s.get("mem_gb") is None:
                s["mem_gb"] = round(est["peak_gb"] * 1.1, 2) if est else a["default_mem_gb"]
                s["mem_source"] = "history" if est else "default"
            else:
                s["mem_source"] = "spec"
            out.append(s)
        return out

    def dry_run(self, plan: Plan) -> dict:
        steps = self.enrich(plan)
        sysinfo = psutil.virtual_memory()
        for s in steps:
            prof = self.profiles.get(s["profile"], {})
            s["cores"] = format_cores(parse_cores(prof.get("cores", "all"), NCPU), NCPU)
            try:
                env = launch.build_env({**s, "run_name": plan.name}, s["threads"])
                cmd = launch.prepare_cmd(s["cmd"], s["threads"])
                s["program"] = "(cmd.exe)" if s["shell"] else (launch.resolve_program(cmd, env) or "NOT FOUND")
                s["final_cmd"] = cmd
            except Exception as e:  # noqa: BLE001 - report, don't crash a dry run
                s["program"] = f"ERROR: {e}"
        run_id = "dryrun"
        durations = {s["key"]: (s["history"]["median_s"] if s["history"] else DEFAULT_DURATION_S) for s in steps}
        sim_steps = [{**s, "run_id": run_id, "state": "waiting", "run_max_parallel": plan.max_parallel}
                     for s in steps]
        ctx = self._ctx_base(sysinfo.available / GB, sysinfo.total / GB)
        now = time.time()
        proj = simulate(sim_steps, now, ctx, durations)
        for s in steps:
            a, b = proj.get(s["key"], (now, now))
            s["proj_start"], s["proj_end"] = a, b
            s["duration_known"] = bool(s["history"])
        problems = [f"{s['key']}: program not found ({s['cmd'].split()[0]})" for s in steps
                    if s.get("program") == "NOT FOUND"]
        problems += [f"{s['key']}: {s['program']}" for s in steps if str(s.get("program", "")).startswith("ERROR")]
        return {"name": plan.name, "priority": plan.priority, "max_parallel": plan.max_parallel,
                "spec_path": plan.spec_path, "warnings": plan.warnings, "problems": problems, "steps": steps,
                "eta": max((s["proj_end"] for s in steps), default=now), "now": now,
                "unknown_durations": sum(1 for s in steps if not s["history"])}

    def submit(self, plan: Plan, submitted_by: str = "") -> str:
        steps = self.enrich(plan)
        run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.store.add_run({"id": run_id, "name": plan.name, "description": plan.description,
                            "spec_path": plan.spec_path, "spec_text": plan.spec_text, "submitted_at": time.time(),
                            "submitted_by": submitted_by, "priority": plan.priority,
                            "max_parallel": plan.max_parallel}, steps)
        log.info("submitted run %s (%s) with %d steps", run_id, plan.name, len(steps))
        return run_id

    # --------------------------------------------------------------- controls
    def cancel(self, run_id: str, key: str | None = None) -> None:
        for s in self.store.steps(run_id):
            if key and s["key"] != key:
                continue
            if s["state"] == "running":
                self.cancel_requested.add((run_id, s["key"]))
                self._kill_step(s)
            elif s["state"] not in FINAL:
                self.store.update_step(run_id, s["key"], state="cancelled", held_reason=None)
        if not key:
            self.store.update_run(run_id, state="cancelled", finished_at=time.time())

    def pause(self, run_id: str, paused: bool) -> None:
        self.store.update_run(run_id, paused=int(paused))

    def set_priority(self, run_id: str, prio: int) -> None:
        self.store.update_run(run_id, priority=int(prio))

    # ---- re-planning waiting steps (queue order, dependencies, concurrency)
    def move_step(self, run_id: str, key: str, before: str | None = None, rewire: bool = True) -> dict:
        order, changes = replan.plan_move(self.store.steps(run_id), key, before, rewire)
        self.store.apply_layout(run_id, order, changes)
        return {"order": order, "needs_changed": changes}

    def set_needs(self, run_id: str, key: str, needs: list[str]) -> dict:
        changes = replan.plan_needs(self.store.steps(run_id), key, needs)
        self.store.apply_layout(run_id, None, changes)
        return {"needs_changed": changes}

    def make_parallel(self, run_id: str, keys: list[str]) -> dict:
        steps = self.store.steps(run_id)
        changes = replan.plan_parallel(steps, keys)
        self.store.apply_layout(run_id, None, changes)
        out: dict = {"needs_changed": changes}
        # A run-wide cap below the group size would still serialise them, defeating the request: raise it.
        cap = (self.store.run(run_id) or {}).get("max_parallel")
        want = sum(1 for s in steps if s["state"] == "running") + len(set(keys))
        if cap and cap < want:
            self.store.update_run(run_id, max_parallel=want)
            out["max_parallel_raised"] = {"from": cap, "to": want}
        return out

    def set_max_parallel(self, run_id: str, n: int | None) -> None:
        if n is not None and int(n) < 1:
            n = None  # 0 / none = no per-run limit (the machine-wide RAM/CPU budget still applies)
        self.store.update_run(run_id, max_parallel=None if n is None else int(n))

    def retry(self, run_id: str, key: str | None = None) -> int:
        n = 0
        for s in self.store.steps(run_id):
            if (key is None or s["key"] == key) and s["state"] in ("failed", "cancelled", "skipped"):
                self.store.update_step(run_id, s["key"], state="waiting", attempt=0, error=None, exit_code=None,
                                       held_reason=None)
                n += 1
        if n:
            self.store.update_run(run_id, state="queued", finished_at=None, hidden=0)
        return n

    def forget(self, run_ids: list[str]) -> list[str]:
        """Hide finished runs from the run lists. Their steps stay in the database, so past runtimes and peak RAM
        still feed estimates, and `status <id>`, `logs` and `retry` still work. Refuses queued/running runs."""
        active = [i for i in run_ids if (self.store.run(i) or {}).get("state") in ("queued", "running")]
        if active:
            raise ValueError(f"cancel these first, they are still active: {', '.join(active)}")
        self.store.set_hidden(run_ids, True)
        return run_ids

    def clear(self, states: list[str]) -> list[str]:
        """Forget every listed run whose state is in `states` (finished states only)."""
        states = [s for s in states if s in ("done", "failed", "cancelled")]
        ids = [r["id"] for r in self.store.runs(100000) if r["state"] in states]
        self.store.set_hidden(ids, True)
        return ids

    def _kill_step(self, s: dict) -> None:
        job = win32.Job.open(s["job_name"]) if s.get("job_name") else None
        if job:
            try:
                job.terminate(1)
                return
            except OSError as e:
                log.warning("terminate job %s: %s", s["job_name"], e)
            finally:
                job.close()
        self._kill_tree(s["pid"])

    def _kill_tree(self, pid: int | None) -> None:
        if not pid:
            return
        try:
            p = psutil.Process(pid)
            procs = p.children(recursive=True) + [p]
        except psutil.Error:
            return
        for c in procs:
            try:
                c.kill()
            except psutil.Error:
                pass

    # --------------------------------------------------------------- tick
    def _refresh_managed(self) -> None:
        self.managed = {s["pid"]: {"run": s["run_name"], "run_id": s["run_id"], "step": s["key"], "cmd": s["cmd"],
                                   "cwd": s["cwd"], "profile": s["profile"], "job_name": s["job_name"]}
                        for s in self.store.running_steps() if s["pid"]}

    def _ctx_base(self, avail_gb: float, total_gb: float) -> dict:
        a = self.settings["admission"]
        return {"avail_gb": avail_gb, "total_gb": total_gb, "headroom_gb": a["headroom_gb"], "outstanding_gb": 0.0,
                "threads_budget": a["cpu_threads_budget"], "max_running": a.get("max_running"), "running": 0,
                "threads_running": 0, "run_running": {}, "step_running": {}}

    @staticmethod
    def _alive(pid: int | None, ctime: float | None) -> bool:
        if not pid:
            return False
        try:
            p = psutil.Process(pid)
            return ctime is None or abs(p.create_time() - ctime) < 1.5
        except psutil.Error:
            return False

    @staticmethod
    def _read_status(path: str | None) -> dict:
        if not path or not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def tick(self, snap: dict, alert: dict) -> None:
        now = snap["t"]
        sy = snap["system"]
        a = self.settings["admission"]
        groups_by_pid = {g["pid"]: g for g in snap["groups"] if g["kind"] == "managed"}
        dt = self.settings.get("sample_interval", 1.0)

        if sy["cpu"] >= a["idle_cpu_below"]:
            self.cpu_busy_t = now
        machine_idle = now - self.cpu_busy_t >= a["idle_minutes"] * 60

        # alert-driven pause / suspend
        level = alert.get("level", "ok")
        actions = self.settings["alerts"].get("on_critical") or []
        if level == "critical" and "pause_admission" in actions:
            self.alert_pause = True
        elif level == "ok":
            self.alert_pause = False
        if level == "critical" and "suspend_lowest" in actions:
            self._suspend_lowest()
        elif level == "ok" and self.alert_suspended:
            self._resume_alert_suspended()

        # ---- monitor running steps
        for s in self.store.running_steps():
            rid, key = s["run_id"], s["key"]
            st = self._read_status(s["status_path"])
            g = groups_by_pid.get(s["pid"])
            upd = {}
            if g:
                upd["peak_rss"] = max(s["peak_rss"] or 0, g["rss"])
                upd["cpu_seconds"] = (s["cpu_seconds"] or 0) + g["cpu"] * NCPU / 100 * dt
            if st.get("child_pid") and not s["child_pid"]:
                upd["child_pid"] = st["child_pid"]
            alive = self._alive(s["pid"], s["pid_ctime"])
            if alive and s["timeout_s"] and now - (s["started_at"] or now) > s["timeout_s"]:
                self._kill_step(s)
                upd.update(error=f"timeout after {s['timeout_s'] / 60:.0f} min")
                self.cancel_requested.discard((rid, key))
                alive = False
                st = {"exit_code": None}
                s["error"] = upd["error"]
            if alive:
                if upd:
                    self.store.update_step(rid, key, **upd)
                continue
            # finished
            code = st.get("exit_code")
            err = st.get("error") or upd.get("error") or s.get("error")
            upd["ended_at"] = st.get("ended") or now
            upd["exit_code"] = code
            if (rid, key) in self.cancel_requested:
                self.cancel_requested.discard((rid, key))
                upd.update(state="cancelled", error="cancelled")
            elif code == 0:
                upd.update(state="done", error=None)
            else:
                if code is None and not err:
                    err = "launcher exited without an exit code (killed?)"
                elif not err:
                    err = f"exit code {code}"
                if (s["attempt"] or 0) <= (s["retries"] or 0):
                    upd.update(state="waiting", error=f"{err}; retrying ({s['attempt']}/{s['retries']})")
                else:
                    upd.update(state="failed", error=err)
            self.store.update_step(rid, key, **upd)
            log.info("step %s/%s -> %s (%s)", s["run_name"], key, upd["state"], err or "ok")

        # ---- admission
        active = self.store.active_runs()
        running_all = self.store.running_steps()
        vm_avail = sy["ram_avail"] / GB
        ctx = self._ctx_base(vm_avail, sy["ram_total"] / GB)
        ctx["running"] = len(running_all)
        ctx["threads_running"] = sum(r["threads"] or 1 for r in running_all)
        for r in running_all:
            ctx["run_running"][r["run_id"]] = ctx["run_running"].get(r["run_id"], 0) + 1
            k2 = (r["run_id"], r["name"])
            ctx["step_running"][k2] = ctx["step_running"].get(k2, 0) + 1
            cur = groups_by_pid.get(r["pid"], {}).get("rss", 0) / GB
            ctx["outstanding_gb"] += max(0.0, (r["mem_gb"] or 0) - cur)
        self.admission_info = {
            "paused": self.manual_pause, "alert_paused": self.alert_pause, "running": ctx["running"],
            "threads_running": ctx["threads_running"], "threads_budget": ctx["threads_budget"],
            "free_gb": max(0.0, vm_avail - ctx["headroom_gb"] - ctx["outstanding_gb"]),
            "headroom_gb": ctx["headroom_gb"], "outstanding_gb": ctx["outstanding_gb"], "machine_idle": machine_idle,
        }

        for run in active:
            steps = self.store.steps(run["id"])
            by_key = {s["key"]: s for s in steps}
            for s in steps:
                if s["state"] != "waiting":
                    continue
                ds = deps_state(s, by_key)
                if ds == "blocked":
                    self.store.update_step(run["id"], s["key"], state="skipped", held_reason=None,
                                           error="a dependency failed")
                    s["state"] = "skipped"
                    continue
                reason = None
                if ds == "wait":
                    reason = "waiting for " + ", ".join(n for n in s["needs"] if by_key[n]["state"] != "done")[:120]
                elif s["start_at"] and s["start_at"] > now:
                    reason = "scheduled " + time.strftime("%a %H:%M", time.localtime(s["start_at"]))
                elif s["when_idle"] and not machine_idle:
                    reason = "waiting for the machine to be idle"
                elif run["paused"]:
                    reason = "run paused"
                elif self.manual_pause:
                    reason = "admission paused"
                elif self.alert_pause:
                    reason = "admission paused by memory alert"
                else:
                    cand = {**s, "run_max_parallel": run["max_parallel"]}
                    ok, why = admit(cand, ctx)
                    if ok:
                        if self._launch(run, s):
                            ctx["running"] += 1
                            ctx["threads_running"] += s["threads"] or 1
                            ctx["run_running"][run["id"]] = ctx["run_running"].get(run["id"], 0) + 1
                            k2 = (run["id"], s["name"])
                            ctx["step_running"][k2] = ctx["step_running"].get(k2, 0) + 1
                            ctx["outstanding_gb"] += s["mem_gb"] or 0
                        continue
                    reason = why
                if reason != s["held_reason"]:
                    self.store.update_step(run["id"], s["key"], held_reason=reason)
            self._update_run_state(run)
        self._refresh_managed()

    def _update_run_state(self, run: dict) -> None:
        steps = self.store.steps(run["id"])
        states = [s["state"] for s in steps]
        if any(st == "running" for st in states):
            new = "running"
        elif any(st == "waiting" for st in states):
            new = "running" if any(s["started_at"] for s in steps) else "queued"
        elif any(st in ("failed", "skipped") for st in states):
            new = "failed"
        elif all(st == "cancelled" for st in states):
            new = "cancelled"
        else:
            new = "done"
        if new != run["state"]:
            upd = {"state": new}
            if new in ("done", "failed", "cancelled"):
                upd["finished_at"] = time.time()
                nd = sum(st == "done" for st in states)
                self.notify(f"procdeck: {run['name']} {new}", f"{nd}/{len(states)} steps done")
            self.store.update_run(run["id"], **upd)

    def _launch(self, run: dict, s: dict) -> bool:
        rid, key = run["id"], s["key"]
        prof = self.profiles.get(s["profile"], {})
        threads = s["threads"] or 1
        attempt = (s["attempt"] or 0) + 1
        d = LOG_DIR / rid
        d.mkdir(parents=True, exist_ok=True)
        base = d / safe_name(key)
        try:
            cores = parse_cores(prof.get("cores", "all"), NCPU)
            env = launch.build_env({**s, "run_name": run["name"]}, threads)
            cmd = launch.prepare_cmd(s["cmd"], threads)
            exe = None if s["shell"] else launch.resolve_program(cmd, env)
            if not s["shell"] and not exe:
                raise RuntimeError(f"program not found: {cmd.split()[0]} (env: {s['env'] or 'none'})")
        except Exception as e:  # noqa: BLE001
            self.store.update_step(rid, key, state="failed", error=str(e), attempt=attempt, ended_at=time.time())
            log.warning("cannot launch %s/%s: %s", run["name"], key, e)
            return False
        job_name = f"Local\\procdeck-{rid}-{safe_name(key)}-{attempt}"
        cfg = {"key": f"{run['name']} › {key}", "attempt": attempt, "cmd": cmd, "executable": exe, "cwd": s["cwd"],
               "env": env, "shell": bool(s["shell"]), "threads": threads,
               "cores": cores if len(cores) < NCPU else None, "cores_txt": format_cores(cores, NCPU),
               "priority": prof.get("priority", "normal"),
               "limits": {k: prof[k] for k in ("cpu_weight", "cpu_cap", "mem_cap_gb") if k in prof},
               "log_path": str(base) + ".log", "status_path": str(base) + ".status.json", "job_name": job_name}
        cfg_path = str(base) + ".cfg.json"
        if os.path.exists(cfg["status_path"]):
            os.remove(cfg["status_path"])
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        pyw = str(Path(sys.executable).with_name("pythonw.exe"))
        if not os.path.exists(pyw):
            pyw = sys.executable
        argv = [pyw, "-m", "procdeck.launcher", cfg_path]
        flags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        try:
            p = subprocess.Popen(argv, cwd=str(REPO_DIR), creationflags=flags | CREATE_BREAKAWAY_FROM_JOB,
                                 close_fds=True)
        except OSError:
            p = subprocess.Popen(argv, cwd=str(REPO_DIR), creationflags=flags, close_fds=True)
        try:
            ctime = psutil.Process(p.pid).create_time()
        except psutil.Error:
            ctime = None
        self.store.update_step(rid, key, state="running", attempt=attempt, pid=p.pid, pid_ctime=ctime,
                               child_pid=None, started_at=time.time(), ended_at=None, exit_code=None,
                               held_reason=None, log_path=cfg["log_path"], status_path=cfg["status_path"],
                               job_name=job_name, threads=threads,
                               error=s["error"] if s["error"] and "retrying" in s["error"] else None)
        log.info("launched %s/%s (attempt %d) pid %d", run["name"], key, attempt, p.pid)
        return True

    def _suspend_lowest(self) -> None:
        running = [s for s in self.store.running_steps() if (s["run_id"], s["key"]) not in self.alert_suspended]
        if not running:
            return
        prio = {r["id"]: r["priority"] for r in self.store.active_runs()}
        victim = min(running, key=lambda s: (prio.get(s["run_id"], 0), -(s["started_at"] or 0)))
        for pid in self.sampler.tree_pids(victim["pid"]):
            try:
                psutil.Process(pid).suspend()
            except psutil.Error:
                pass
        self.alert_suspended.add((victim["run_id"], victim["key"]))
        self.store.update_step(victim["run_id"], victim["key"], held_reason="suspended by memory alert")
        self.notify("procdeck: suspended a step", f"{victim['run_name']} › {victim['key']} (RAM critical)")

    def _resume_alert_suspended(self) -> None:
        for s in self.store.running_steps():
            if (s["run_id"], s["key"]) in self.alert_suspended:
                for pid in self.sampler.tree_pids(s["pid"]):
                    try:
                        psutil.Process(pid).resume()
                    except psutil.Error:
                        pass
                self.store.update_step(s["run_id"], s["key"], held_reason=None)
        self.alert_suspended.clear()

    # --------------------------------------------------------------- views
    def runs_view(self, limit: int = 30, include_hidden: bool = False) -> list[dict]:
        out = []
        now = time.time()
        for r in self.store.runs(limit, include_hidden):
            steps = self.store.steps(r["id"])
            counts: dict[str, int] = {}
            for s in steps:
                counts[s["state"]] = counts.get(s["state"], 0) + 1
            r["counts"] = counts
            r["n_steps"] = len(steps)
            if r["state"] in ("queued", "running"):
                r["eta"] = self._project(r, steps, now)
            out.append(r)
        return out

    def _project(self, run: dict, steps: list[dict], now: float) -> float | None:
        durations = {}
        for s in steps:
            est = self.store.estimate(s["script_key"])
            durations[s["key"]] = est["median_s"] if est else DEFAULT_DURATION_S
        sim = [{**s, "threads": s["threads"] or 1, "mem_gb": s["mem_gb"] or 0, "run_max_parallel": run["max_parallel"]}
               for s in steps]
        vm = psutil.virtual_memory()
        proj = simulate(sim, now, self._ctx_base(vm.available / GB, vm.total / GB), durations)
        run["_proj"] = proj
        return max((b for a, b in proj.values()), default=None)

    def run_detail(self, run_id: str) -> dict | None:
        r = self.store.run(run_id)
        if not r:
            return None
        steps = self.store.steps(run_id)
        now = time.time()
        if r["state"] in ("queued", "running"):
            r["eta"] = self._project(r, steps, now)
            proj = r.pop("_proj")
        else:
            proj = {}
        for s in steps:
            est = self.store.estimate(s["script_key"])
            s["history"] = est
            if s["key"] in proj:
                s["proj_start"], s["proj_end"] = proj[s["key"]]
            s.pop("env_vars", None)
        r["steps"] = steps
        return r

    def log_tail(self, run_id: str, key: str, lines: int = 200) -> str:
        s = next((x for x in self.store.steps(run_id) if x["key"] == key), None)
        if not s or not s["log_path"] or not os.path.exists(s["log_path"]):
            return ""
        with open(s["log_path"], "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            data = f.read().decode("utf-8", "replace")
        return "\n".join(data.replace("\r\n", "\n").split("\n")[-lines:])
