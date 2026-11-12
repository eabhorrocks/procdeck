"""Per-step launcher: `pythonw -m procdeck.launcher <config.json>`.

Puts itself in a fresh job object (so every process the step spawns inherits the CPU/affinity/memory limits),
starts the command with output going to the step's log, waits, and records the exit code in a status file.
It stays alive for the step's lifetime; the daemon watches it by PID.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time

import psutil

from . import win32

CREATE_NO_WINDOW = 0x08000000
PRIORITY = {"idle": psutil.IDLE_PRIORITY_CLASS, "below_normal": psutil.BELOW_NORMAL_PRIORITY_CLASS,
            "normal": psutil.NORMAL_PRIORITY_CLASS, "above_normal": psutil.ABOVE_NORMAL_PRIORITY_CLASS,
            "high": psutil.HIGH_PRIORITY_CLASS}


def write_status(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def main(cfg_path: str) -> int:
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    status = {"launcher_pid": os.getpid(), "child_pid": None, "started": time.time(), "exit_code": None,
              "ended": None, "error": None, "warnings": []}
    log = open(cfg["log_path"], "ab", buffering=0)

    def note(msg: str) -> None:
        log.write(f"[procdeck] {msg}\r\n".encode("utf-8", "replace"))

    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    note(f"=== {cfg['key']} attempt {cfg.get('attempt', 1)} started {stamp} ===")
    note(f"cwd: {cfg['cwd']}")
    note(f"cmd: {cfg['cmd']}")
    note(f"program: {cfg.get('executable') or '(via cmd.exe)'}  threads: {cfg['threads']}  "
         f"cores: {cfg.get('cores_txt', 'all')}  priority: {cfg.get('priority', 'normal')}")

    job = None
    try:
        job = win32.Job.create(cfg["job_name"])
        status["warnings"] += job.apply_profile(cfg.get("limits", {}), cfg.get("cores"))
        job.assign_current_process()
    except OSError as e:
        status["warnings"].append(f"job object: {e}")
        job = None
        if cfg.get("cores"):
            try:
                psutil.Process().cpu_affinity(cfg["cores"])
            except psutil.Error as e2:
                status["warnings"].append(f"affinity: {e2}")
    pri = PRIORITY.get(cfg.get("priority", "normal"))
    try:
        if pri is not None:
            psutil.Process().nice(pri)  # idle/below_normal are inherited by children
    except psutil.Error as e:
        status["warnings"].append(f"priority: {e}")
    for w in status["warnings"]:
        note(f"warning: {w}")
    write_status(cfg["status_path"], status)

    try:
        if cfg.get("shell"):
            proc = subprocess.Popen(cfg["cmd"], cwd=cfg["cwd"], env=cfg["env"], stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, shell=True, creationflags=CREATE_NO_WINDOW)
        else:
            proc = subprocess.Popen(cfg["cmd"], executable=cfg["executable"], cwd=cfg["cwd"], env=cfg["env"],
                                    stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    creationflags=CREATE_NO_WINDOW)
    except OSError as e:
        note(f"failed to start: {e}")
        status.update(exit_code=-1, ended=time.time(), error=f"failed to start: {e}")
        write_status(cfg["status_path"], status)
        return 1

    status["child_pid"] = proc.pid
    try:
        cp = psutil.Process(proc.pid)
        status["child_ctime"] = cp.create_time()
        if pri is not None and pri not in (psutil.IDLE_PRIORITY_CLASS, psutil.BELOW_NORMAL_PRIORITY_CLASS):
            cp.nice(pri)
    except psutil.Error:
        pass
    write_status(cfg["status_path"], status)

    rc = proc.wait()
    status.update(exit_code=rc, ended=time.time())
    write_status(cfg["status_path"], status)
    note(f"=== exited with code {rc} at {dt.datetime.now():%H:%M:%S} ===")
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
