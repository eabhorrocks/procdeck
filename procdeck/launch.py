"""Build the launch config for a step (env vars, thread caps, resolved program)."""
from __future__ import annotations

import ntpath
import os
import shutil

from . import condaenv, labels
from .config import parse_cores

THREAD_VARS = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS",
               "NUMEXPR_MAX_THREADS", "NUMBA_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "LOKY_MAX_CPU_COUNT",
               "PROCDECK_WORKERS")


def step_threads(step: dict, profile: dict, ncpu: int) -> int:
    if step.get("threads"):
        return int(step["threads"])
    if profile.get("threads"):
        return int(profile["threads"])
    return len(parse_cores(profile.get("cores", "all"), ncpu))


def build_env(step: dict, threads: int, base_env: dict | None = None) -> dict:
    env = condaenv.activation_env(step["env"], base_env) if step.get("env") else dict(base_env or os.environ)
    for v in THREAD_VARS:
        env[v] = str(threads)
    env["PYTHONUNBUFFERED"] = "1"  # so logs stream instead of arriving at exit
    env["PROCDECK_RUN"] = str(step.get("run_name", ""))
    env["PROCDECK_STEP"] = step["key"]
    for k, v in (step.get("env_vars") or {}).items():
        env[k] = v
    return env


def resolve_program(cmd: str, env: dict) -> str | None:
    """Absolute path of the program a command line will run, searched on the *step's* PATH.

    Windows would otherwise search the launcher's own directory first (the procdeck env), so
    `python` would run procdeck's python instead of the step env's.
    """
    prog, _ = labels.split_first_token(cmd)
    if not prog:
        return None
    if ntpath.isabs(prog) and os.path.exists(prog):
        return prog
    path = next((v for k, v in env.items() if k.upper() == "PATH"), None)
    return shutil.which(prog, path=path)


def prepare_cmd(cmd: str, threads: int) -> str:
    prog, _ = labels.split_first_token(cmd)
    if ntpath.basename(prog).lower() in ("matlab", "matlab.exe"):
        return labels.inject_matlab_threads(cmd, threads)
    return cmd
