"""Paths and YAML config loading."""
from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Any

import yaml

REPO_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("PROCDECK_CONFIG", REPO_DIR / "config"))
DATA_DIR = Path(os.environ.get("PROCDECK_DATA", Path(os.environ.get("LOCALAPPDATA", Path.home())) / "procdeck"))
LOG_DIR = DATA_DIR / "logs"
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "procdeck.sqlite"
TOKEN_PATH = DATA_DIR / "token"
DAEMON_LOG = DATA_DIR / "daemon.log"
WEB_DIR = REPO_DIR / "web"

CONFIG_FILES = ("settings", "profiles", "rules")

DEFAULT_SETTINGS: dict[str, Any] = {
    "port": 8857,
    "sample_interval": 1.0,
    "history_interval": 5,
    "history_days": 30,
    "alerts": {"ram_warn": 85, "ram_critical": 93, "commit_warn": 90, "eta_warn_minutes": 5,
               "toast": True, "on_critical": ["pause_admission"]},
    "flags": {"idle_cpu_below": 1.0, "idle_minutes": 30, "idle_ram_gb": 1.0,
              "orphan_watch": ["python.exe", "pythonw.exe", "matlab.exe"]},
    "admission": {"headroom_gb": 1.5, "default_mem_gb": 2.0, "cpu_threads_budget": os.cpu_count() or 4,
                  "max_running": None, "idle_cpu_below": 20, "idle_minutes": 5},
    "compute_exes": ["python.exe", "pythonw.exe", "matlab.exe"],
}

PRIORITIES = ("idle", "below_normal", "normal", "above_normal", "high")


def ensure_dirs() -> None:
    for d in (DATA_DIR, LOG_DIR, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def get_token() -> str:
    ensure_dirs()
    if TOKEN_PATH.exists():
        tok = TOKEN_PATH.read_text().strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(24)
    TOKEN_PATH.write_text(tok)
    return tok


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def config_path(name: str) -> Path:
    if name not in CONFIG_FILES:
        raise ValueError(f"unknown config file {name!r}")
    return CONFIG_DIR / f"{name}.yaml"


def load_settings() -> dict:
    p = config_path("settings")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    s = _deep_merge(DEFAULT_SETTINGS, data or {})
    s["compute_exes"] = [e.lower() for e in s["compute_exes"]]
    s["flags"]["orphan_watch"] = [e.lower() for e in s["flags"]["orphan_watch"]]
    return s


def load_profiles() -> dict[str, dict]:
    p = config_path("profiles")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    data = data or {}
    data.setdefault("default", {"cores": "all", "priority": "normal"})
    for name, prof in data.items():
        validate_profile(name, prof)
    return data


def load_rules() -> list[dict]:
    p = config_path("rules")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else []
    data = data or []
    if not isinstance(data, list):
        raise ValueError("rules.yaml must be a list")
    for i, r in enumerate(data):
        if not isinstance(r, dict) or "match" not in r or "set" not in r:
            raise ValueError(f"rule {i + 1}: needs 'match' and 'set'")
    return data


def validate_config_text(name: str, text: str) -> None:
    """Raise ValueError if `text` is not a valid version of config file `name`."""
    data = yaml.safe_load(text)
    if name == "profiles":
        for pname, prof in (data or {}).items():
            validate_profile(pname, prof)
    elif name == "rules":
        if data is not None and not isinstance(data, list):
            raise ValueError("rules.yaml must be a list")
        for i, r in enumerate(data or []):
            if not isinstance(r, dict) or "match" not in r or "set" not in r:
                raise ValueError(f"rule {i + 1}: needs 'match' and 'set'")
    elif name == "settings":
        if data is not None and not isinstance(data, dict):
            raise ValueError("settings.yaml must be a mapping")


PROFILE_KEYS = {"cores", "threads", "priority", "cpu_weight", "cpu_cap", "mem_cap_gb"}


def validate_profile(name: str, prof: dict) -> None:
    if not isinstance(prof, dict):
        raise ValueError(f"profile {name!r} must be a mapping")
    unknown = set(prof) - PROFILE_KEYS
    if unknown:
        raise ValueError(f"profile {name!r}: unknown keys {sorted(unknown)}")
    if "cores" in prof:
        parse_cores(prof["cores"])
    if "priority" in prof and prof["priority"] not in PRIORITIES:
        raise ValueError(f"profile {name!r}: priority must be one of {PRIORITIES}")
    if "cpu_weight" in prof and not (1 <= int(prof["cpu_weight"]) <= 9):
        raise ValueError(f"profile {name!r}: cpu_weight must be 1-9")
    if "cpu_cap" in prof and not (1 <= float(prof["cpu_cap"]) <= 100):
        raise ValueError(f"profile {name!r}: cpu_cap must be 1-100")
    if "cpu_cap" in prof and "cpu_weight" in prof:
        raise ValueError(f"profile {name!r}: use cpu_weight or cpu_cap, not both")


def parse_cores(spec: Any, ncpu: int | None = None) -> list[int]:
    """'all' | '0-5' | '0,2,4-6' | [0,1] -> sorted list of logical CPU indices."""
    ncpu = ncpu or os.cpu_count() or 1
    if spec is None or spec == "all":
        return list(range(ncpu))
    if isinstance(spec, int):
        spec = str(spec)
    if isinstance(spec, (list, tuple)):
        cores = {int(c) for c in spec}
    else:
        cores = set()
        for part in str(spec).replace(" ", "").split(","):
            if not part:
                continue
            m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
            if not m:
                raise ValueError(f"bad cores spec {spec!r}")
            a, b = int(m.group(1)), int(m.group(2) or m.group(1))
            if b < a:
                raise ValueError(f"bad cores range {part!r}")
            cores.update(range(a, b + 1))
    bad = [c for c in cores if c < 0 or c >= ncpu]
    if bad:
        raise ValueError(f"cores {bad} out of range (this machine has 0-{ncpu - 1})")
    if not cores:
        raise ValueError("empty cores spec")
    return sorted(cores)


def format_cores(cores: list[int], ncpu: int | None = None) -> str:
    ncpu = ncpu or os.cpu_count() or 1
    if not cores:
        return "-"
    if sorted(cores) == list(range(ncpu)):
        return "all"
    parts, start, prev = [], cores[0], cores[0]
    for c in cores[1:] + [None]:
        if c is not None and c == prev + 1:
            prev = c
            continue
        parts.append(f"{start}-{prev}" if prev != start else f"{start}")
        if c is not None:
            start = prev = c
    return ",".join(parts)


_SIZE = re.compile(r"^\s*([\d.]+)\s*([kKmMgGtT]?)[bB]?\s*$")


def parse_size_gb(v: Any) -> float | None:
    """'6G' | '512M' | 6 (GB) -> GB as float."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = _SIZE.match(str(v))
    if not m:
        raise ValueError(f"bad size {v!r} (use e.g. 6G or 512M)")
    n, unit = float(m.group(1)), m.group(2).upper()
    return n * {"": 1, "K": 1 / 1024 ** 2, "M": 1 / 1024, "G": 1, "T": 1024}[unit]


_DUR = re.compile(r"(\d+(?:\.\d+)?)\s*([hms]?)")


def parse_duration_s(v: Any) -> float | None:
    """'2h' | '90m' | '1h30m' | 3600 -> seconds."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower()
    total, pos = 0.0, 0
    for m in _DUR.finditer(s):
        if s[pos:m.start()].strip():
            raise ValueError(f"bad duration {v!r}")
        total += float(m.group(1)) * {"h": 3600, "m": 60, "s": 1, "": 1}[m.group(2)]
        pos = m.end()
    if pos == 0 or s[pos:].strip():
        raise ValueError(f"bad duration {v!r} (use e.g. 2h, 30m, 1h30m)")
    return total
