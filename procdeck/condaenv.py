"""Resolve conda environments to the environment variables an activated shell would have."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .config import CACHE_DIR, ensure_dirs

log = logging.getLogger("procdeck")
CREATE_NO_WINDOW = 0x08000000
_lock = threading.Lock()
_envs_cache: tuple[float, dict[str, str]] | None = None


def conda_exe() -> str | None:
    cands = [os.environ.get("CONDA_EXE"), shutil.which("conda")]
    for root in (os.environ.get("LOCALAPPDATA"), str(Path.home()), os.environ.get("ProgramData")):
        if root:
            for d in ("miniconda3", "anaconda3", "miniforge3", "mambaforge"):
                cands.append(str(Path(root) / d / "Scripts" / "conda.exe"))
    for c in cands:
        if c and Path(c).exists():
            return c
    return None


def list_envs(max_age: float = 60) -> dict[str, str]:
    """{name: prefix}, including 'base'."""
    global _envs_cache
    with _lock:
        if _envs_cache and time.time() - _envs_cache[0] < max_age:
            return _envs_cache[1]
        exe = conda_exe()
        envs: dict[str, str] = {}
        if exe:
            root = str(Path(exe).resolve().parent.parent)
            envs["base"] = root
            try:
                out = subprocess.run([exe, "env", "list", "--json"], capture_output=True, text=True, timeout=60,
                                     creationflags=CREATE_NO_WINDOW).stdout
                for p in json.loads(out).get("envs", []):
                    if os.path.normcase(p) != os.path.normcase(root):
                        envs[Path(p).name] = p
            except Exception as e:  # fall back to scanning envs/
                log.warning("conda env list failed: %s", e)
                for d in (Path(root) / "envs").glob("*"):
                    if (d / "conda-meta").is_dir():
                        envs[d.name] = str(d)
        _envs_cache = (time.time(), envs)
        return envs


def env_exists(name: str) -> bool:
    return name in list_envs()


def _manual_activation(prefix: str, name: str, base_env: dict) -> dict:
    p = Path(prefix)
    dirs = [p, p / "Library" / "mingw-w64" / "bin", p / "Library" / "usr" / "bin", p / "Library" / "bin",
            p / "Scripts", p / "bin"]
    env = dict(base_env)
    env["PATH"] = os.pathsep.join(str(d) for d in dirs) + os.pathsep + base_env.get("PATH", "")
    env["CONDA_PREFIX"] = str(p)
    env["CONDA_DEFAULT_ENV"] = name
    return env


def activation_env(name: str, base_env: dict | None = None) -> dict:
    """Environment variables after `conda activate name`. Cached until the env's packages change."""
    base_env = dict(base_env or os.environ)
    envs = list_envs()
    if name not in envs:
        raise ValueError(f"conda env {name!r} not found")
    prefix = envs[name]
    hist = Path(prefix) / "conda-meta" / "history"
    stamp = hist.stat().st_mtime if hist.exists() else 0
    ensure_dirs()
    cache = CACHE_DIR / f"env_{name}.json"
    if cache.exists():
        try:
            c = json.loads(cache.read_text(encoding="utf-8"))
            if c.get("stamp") == stamp and c.get("prefix") == prefix:
                return _overlay(base_env, c["delta"])
        except (ValueError, KeyError):
            pass
    exe = conda_exe()
    full = None
    if exe:
        try:
            r = subprocess.run([exe, "run", "-n", name, "cmd", "/d", "/c", "set"], capture_output=True,
                               text=True, timeout=120, env=base_env, creationflags=CREATE_NO_WINDOW)
            if r.returncode == 0 and "PATH=" in r.stdout.upper():
                full = {}
                for line in r.stdout.splitlines():
                    if "=" in line and not line.startswith("="):
                        k, v = line.split("=", 1)
                        full[k] = v
        except Exception as e:
            log.warning("conda run activation for %s failed: %s", name, e)
    if full is None:
        full = _manual_activation(prefix, name, base_env)
    # store only what activation changed, so later base env changes still flow through
    upper_base = {k.upper(): v for k, v in base_env.items()}
    delta = {k: v for k, v in full.items() if upper_base.get(k.upper()) != v and not k.startswith("CONDA_EXE_")}
    for junk in ("PROMPT", "_CE_M", "_CE_CONDA"):
        delta.pop(junk, None)
    cache.write_text(json.dumps({"stamp": stamp, "prefix": prefix, "delta": delta}), encoding="utf-8")
    return _overlay(base_env, delta)


def _overlay(base: dict, delta: dict) -> dict:
    env = dict(base)
    up = {k.upper(): k for k in env}
    for k, v in delta.items():
        if k.upper() in up:
            env[up[k.upper()]] = v
        else:
            env[k] = v
    return env
