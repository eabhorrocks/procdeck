"""Pipeline spec: parse, validate and expand YAML into concrete step instances."""
from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from .config import parse_duration_s, parse_size_gb

TOP_KEYS = {"name", "description", "priority", "mode", "max_parallel", "defaults", "steps"}
DEFAULT_KEYS = {"cwd", "env", "profile", "mem", "retries", "timeout", "env_vars", "on_fail", "shell", "threads"}
STEP_KEYS = DEFAULT_KEYS | {"name", "cmd", "needs", "foreach", "start_at", "when", "max_parallel"}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
SUBST_RE = re.compile(r"\{(item(?:\.[A-Za-z0-9_]+)?|index)\}")


class SpecError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass
class Plan:
    name: str
    priority: int
    max_parallel: int | None
    description: str
    spec_path: str | None
    spec_text: str
    steps: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _item_key(item: Any, index: int) -> str:
    if isinstance(item, dict):
        for k in ("name", "id", "key", "session"):
            if k in item:
                return str(item[k])
        return str(index)
    return str(item)


def substitute(text: str, item: Any, index: int | None) -> str:
    if item is None and index is None:
        return text

    def rep(m):
        tok = m.group(1)
        if tok == "index":
            return str(index)
        if tok == "item":
            return str(item)
        key = tok.split(".", 1)[1]
        if not isinstance(item, dict) or key not in item:
            raise KeyError(f"{{{tok}}}: item has no field {key!r}")
        return str(item[key])

    return SUBST_RE.sub(rep, text)


def parse_start_at(v: Any, now: dt.datetime) -> float:
    """'22:00' (today, or tomorrow if already past) or ISO datetime -> unix time."""
    if isinstance(v, dt.datetime):
        return v.timestamp()
    s = str(v).strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if m:
        t = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if t <= now:
            t += dt.timedelta(days=1)
        return t.timestamp()
    try:
        return dt.datetime.fromisoformat(s).timestamp()
    except ValueError:
        raise ValueError(f"bad start_at {v!r} (use HH:MM or 'YYYY-MM-DD HH:MM')")


def default_cwd(spec_path: str | None) -> str:
    if not spec_path:
        return os.getcwd()
    d = Path(spec_path).resolve().parent
    return str(d.parent if d.name.lower() == "jobs" else d)


def parse(text: str, spec_path: str | None, profiles: dict, env_exists: Callable[[str], bool] | None = None,
          now: dt.datetime | None = None) -> Plan:
    """Validate and expand. Raises SpecError listing every problem found."""
    now = now or dt.datetime.now()
    errors: list[str] = []
    warnings: list[str] = []
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise SpecError([f"YAML syntax: {e}"])
    if not isinstance(data, dict):
        raise SpecError(["spec must be a mapping with 'name' and 'steps'"])

    unknown = set(data) - TOP_KEYS
    if unknown:
        errors.append(f"unknown top-level keys {sorted(unknown)} (allowed: {sorted(TOP_KEYS)})")
    name = data.get("name")
    if not name or not NAME_RE.match(str(name)):
        errors.append("'name' is required: letters, digits, _ . -")
    mode = data.get("mode", "parallel")
    if mode not in ("parallel", "sequential"):
        errors.append("mode must be 'parallel' or 'sequential'")
    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        errors.append("'defaults' must be a mapping")
        defaults = {}
    bad = set(defaults) - DEFAULT_KEYS
    if bad:
        errors.append(f"unknown keys in defaults: {sorted(bad)} (allowed: {sorted(DEFAULT_KEYS)})")
    steps_in = data.get("steps")
    if not isinstance(steps_in, list) or not steps_in:
        errors.append("'steps' must be a non-empty list")
        raise SpecError(errors)

    base_cwd = default_cwd(spec_path)
    step_names: list[str] = []
    for i, s in enumerate(steps_in):
        if not isinstance(s, dict):
            errors.append(f"step {i + 1}: must be a mapping")
            continue
        sn = s.get("name")
        if not sn or not NAME_RE.match(str(sn)):
            errors.append(f"step {i + 1}: 'name' is required (letters, digits, _ . -)")
        elif sn in step_names:
            errors.append(f"step {sn!r}: duplicate name")
        step_names.append(str(sn))
    if errors:
        raise SpecError(errors)

    instances: list[dict] = []
    by_step: dict[str, list[str]] = {}
    raw_needs: dict[str, list[str]] = {}
    for i, s in enumerate(steps_in):
        sn = str(s["name"])
        where = f"step {sn!r}"
        bad = set(s) - STEP_KEYS
        if bad:
            errors.append(f"{where}: unknown keys {sorted(bad)} (allowed: {sorted(STEP_KEYS)})")
        if not s.get("cmd"):
            errors.append(f"{where}: 'cmd' is required")
            continue
        merged = {**defaults, **{k: v for k, v in s.items() if k in DEFAULT_KEYS}}

        # parse fields shared by all instances
        try:
            mem = parse_size_gb(merged.get("mem"))
        except ValueError as e:
            errors.append(f"{where}: {e}")
            mem = None
        try:
            timeout = parse_duration_s(merged.get("timeout"))
        except ValueError as e:
            errors.append(f"{where}: {e}")
            timeout = None
        retries = merged.get("retries", 0)
        if not isinstance(retries, int) or retries < 0:
            errors.append(f"{where}: retries must be an integer >= 0")
            retries = 0
        on_fail = merged.get("on_fail", "stop")
        if on_fail not in ("stop", "continue"):
            errors.append(f"{where}: on_fail must be 'stop' or 'continue'")
        profile = merged.get("profile", "default")
        if profile not in profiles:
            errors.append(f"{where}: unknown profile {profile!r} (have: {sorted(profiles)})")
        threads = merged.get("threads")
        if threads is not None and (not isinstance(threads, int) or threads < 1):
            errors.append(f"{where}: threads must be a positive integer")
        env = merged.get("env")
        if env and env_exists and not env_exists(str(env)):
            errors.append(f"{where}: conda env {env!r} not found")
        env_vars = merged.get("env_vars") or {}
        if not isinstance(env_vars, dict):
            errors.append(f"{where}: env_vars must be a mapping")
            env_vars = {}
        start_at = None
        if s.get("start_at") is not None:
            try:
                start_at = parse_start_at(s["start_at"], now)
            except ValueError as e:
                errors.append(f"{where}: {e}")
        when = s.get("when")
        if when not in (None, "idle"):
            errors.append(f"{where}: when must be 'idle' if given")
        smax = s.get("max_parallel")
        if smax is not None and (not isinstance(smax, int) or smax < 1):
            errors.append(f"{where}: max_parallel must be a positive integer")
        needs = s.get("needs")
        if needs is None and mode == "sequential" and i > 0:
            needs = [step_names[i - 1]]
        if isinstance(needs, str):
            needs = [needs]
        if needs is not None and not isinstance(needs, list):
            errors.append(f"{where}: needs must be a list of step names")
            needs = []

        foreach = s.get("foreach")
        if foreach is not None and (not isinstance(foreach, list) or not foreach):
            errors.append(f"{where}: foreach must be a non-empty list")
            foreach = None
        items = list(enumerate(foreach)) if foreach else [(None, None)]
        keys_seen: set[str] = set()
        by_step[sn] = []
        for idx, item in items:
            key = sn if idx is None else f"{sn}[{_item_key(item, idx)}]"
            if key in keys_seen:
                errors.append(f"{where}: foreach items give duplicate key {key!r}")
                continue
            keys_seen.add(key)
            try:
                cmd = substitute(str(s["cmd"]), item, idx)
                cwd_raw = substitute(str(merged.get("cwd") or base_cwd), item, idx)
                evars = {str(k): substitute(str(v), item, idx) for k, v in env_vars.items()}
                inst_needs = [substitute(str(n), item, idx) for n in (needs or [])]
            except KeyError as e:
                errors.append(f"{where}: {e.args[0]}")
                continue
            cwd_p = Path(cwd_raw)
            if not cwd_p.is_absolute():
                cwd_p = (Path(spec_path).resolve().parent if spec_path else Path.cwd()) / cwd_p
            cwd = str(cwd_p.resolve())
            if not cwd_p.is_dir():
                errors.append(f"{where}: cwd does not exist: {cwd}")
            instances.append({
                "key": key, "name": sn, "item": None if idx is None else _item_key(item, idx),
                "cmd": cmd, "cwd": cwd, "env": str(env) if env else None, "profile": profile,
                "mem_gb": mem, "threads": threads, "retries": retries, "timeout_s": timeout,
                "on_fail": on_fail, "shell": bool(merged.get("shell", False)), "env_vars": evars,
                "start_at": start_at, "when_idle": when == "idle", "step_max_parallel": smax,
                "needs": [], "seq": len(instances),
            })
            raw_needs[key] = inst_needs
            by_step[sn].append(key)

    # resolve needs: step name -> all its instances; or an explicit instance key
    all_keys = {inst["key"] for inst in instances}
    for inst in instances:
        resolved: list[str] = []
        for n in raw_needs.get(inst["key"], []):
            if n in by_step:
                resolved.extend(by_step[n])
            elif n in all_keys:
                resolved.append(n)
            else:
                errors.append(f"step {inst['key']!r}: needs unknown step {n!r}")
        if inst["key"] in resolved:
            errors.append(f"step {inst['key']!r}: needs itself")
        inst["needs"] = sorted(set(resolved), key=lambda k: next(i["seq"] for i in instances if i["key"] == k))

    # cycle check (Kahn)
    if not errors:
        indeg = {i["key"]: len(i["needs"]) for i in instances}
        dependents: dict[str, list[str]] = {}
        for i in instances:
            for n in i["needs"]:
                dependents.setdefault(n, []).append(i["key"])
        queue = [k for k, d in indeg.items() if d == 0]
        done = 0
        while queue:
            k = queue.pop()
            done += 1
            for d in dependents.get(k, []):
                indeg[d] -= 1
                if indeg[d] == 0:
                    queue.append(d)
        if done != len(instances):
            errors.append("dependency cycle between steps: " + ", ".join(k for k, d in indeg.items() if d > 0))

    mp = data.get("max_parallel")
    if mp is not None and (not isinstance(mp, int) or mp < 1):
        errors.append("max_parallel must be a positive integer")
    prio = data.get("priority", 0)
    if not isinstance(prio, int):
        errors.append("priority must be an integer (higher runs first)")
    if errors:
        raise SpecError(errors)
    for inst in instances:
        if inst["env"] is None and re.match(r"\s*\"?python", inst["cmd"], re.I):
            warnings.append(f"{inst['key']}: no conda env given; 'python' resolves from the daemon's PATH")
            break
    return Plan(name=str(name), priority=prio, max_parallel=mp, description=str(data.get("description") or ""),
                spec_path=str(Path(spec_path).resolve()) if spec_path else None, spec_text=text,
                steps=instances, warnings=warnings)
