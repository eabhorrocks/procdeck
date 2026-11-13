"""Re-planning a submitted run: reorder waiting steps and rewire their dependencies.

Pure functions over step dicts (`key`, `state`, `needs`, in queue/`seq` order); nothing here touches the database.
Only steps in state `waiting` can be changed: running/finished steps are history.

Two separate things decide when a waiting step starts:
  * queue order (`seq`): among steps that are ready at the same moment, earlier ones get free slots first;
  * `needs`: a step never starts before everything it needs has finished.
"""
from __future__ import annotations


def _by_key(steps: list[dict]) -> dict[str, dict]:
    return {s["key"]: s for s in steps}


def _require_waiting(by: dict[str, dict], key: str) -> dict:
    s = by.get(key)
    if s is None:
        raise ValueError(f"no step {key!r} in this run")
    if s["state"] != "waiting":
        raise ValueError(f"step {key!r} is {s['state']}; only waiting steps can be changed")
    return s


def has_cycle(needs: dict[str, list[str]]) -> bool:
    indeg = {k: sum(1 for n in v if n in needs) for k, v in needs.items()}
    users: dict[str, list[str]] = {k: [] for k in needs}
    for k, v in needs.items():
        for n in v:
            if n in users:
                users[n].append(k)
    ready = [k for k, d in indeg.items() if d == 0]
    seen = 0
    while ready:
        k = ready.pop()
        seen += 1
        for u in users[k]:
            indeg[u] -= 1
            if indeg[u] == 0:
                ready.append(u)
    return seen != len(needs)


def _depends_on(needs: dict[str, list[str]], start: str, targets: set[str]) -> bool:
    """Does `start` (transitively) need any of `targets`?"""
    stack, seen = list(needs.get(start, [])), set()
    while stack:
        k = stack.pop()
        if k in targets:
            return True
        if k not in seen:
            seen.add(k)
            stack.extend(needs.get(k, []))
    return False


def _sorted(keys, pos: dict[str, int]) -> list[str]:
    return sorted(set(keys), key=lambda k: pos[k])


def _checked(steps: list[dict], changes: dict[str, list[str]]) -> dict[str, list[str]]:
    needs = {s["key"]: list(s["needs"]) for s in steps}
    needs.update(changes)
    if has_cycle(needs):
        raise ValueError("that would make steps wait for each other in a circle")
    return changes


def plan_needs(steps: list[dict], key: str, needs: list[str]) -> dict[str, list[str]]:
    """Make `key` wait for exactly `needs` (empty = start as soon as resources allow)."""
    by = _by_key(steps)
    _require_waiting(by, key)
    pos = {s["key"]: i for i, s in enumerate(steps)}
    for n in needs:
        if n not in by:
            raise ValueError(f"no step {n!r} in this run")
        if n == key:
            raise ValueError("a step cannot wait for itself")
    return _checked(steps, {key: _sorted(needs, pos)})


def plan_parallel(steps: list[dict], keys: list[str]) -> dict[str, list[str]]:
    """Make the given waiting steps independent siblings that can run at the same time.

    They all inherit what the group was waiting for (minus members of the group, and minus anything that itself
    waits on a member), and whatever used to wait for one of them now waits for all of them.
    One key just releases that step: it stops waiting for anything.
    """
    by = _by_key(steps)
    group = set(keys)
    for k in group:
        _require_waiting(by, k)
    pos = {s["key"]: i for i, s in enumerate(steps)}
    needs = {s["key"]: list(s["needs"]) for s in steps}
    if len(group) == 1:
        return _checked(steps, {next(iter(group)): []})
    common = {p for k in group for p in needs[k] if p not in group and not _depends_on(needs, p, group)}
    changes = {k: _sorted(common, pos) for k in group}
    for s in steps:
        k = s["key"]
        if k not in group and s["state"] == "waiting" and group & set(needs[k]):
            changes[k] = _sorted((set(needs[k]) - group) | group, pos)
    return _checked(steps, changes)


def plan_move(steps: list[dict], key: str, before: str | None = None,
              rewire: bool = True) -> tuple[list[str], dict[str, list[str]]]:
    """Move waiting step `key` to sit just before waiting step `before` (None = last).

    Returns (new order of all step keys, {step key: new needs}).

    With `rewire`, a move that crosses one of the step's own dependencies keeps its meaning instead of being
    silently ignored: in a chain A -> B -> C, dragging C above A gives C -> A -> B. Moves that cross no dependency
    (the usual case in a parallel run) only change queue order and leave `needs` alone.
    """
    by = _by_key(steps)
    _require_waiting(by, key)
    if before is not None:
        _require_waiting(by, before)
    order = [s["key"] for s in steps if s["key"] != key]
    i = order.index(before) if before is not None else len(order)
    order.insert(i, key)
    if not rewire or before == key:
        return order, {}

    pos = {k: n for n, k in enumerate(order)}
    waiting = lambda k: by[k]["state"] == "waiting"  # noqa: E731
    needs = {s["key"]: list(s["needs"]) for s in steps}
    up = [p for p in needs[key] if waiting(p) and pos[p] > pos[key]]               # now ahead of something it needs
    down = [k for k, n in needs.items() if key in n and waiting(k) and pos[k] < pos[key]]  # now behind a dependent
    if not (up or down):
        return order, {}

    changes: dict[str, list[str]] = {}
    # 1. splice the step out: whatever waited for it now waits for what it waited for
    for k, n in needs.items():
        if key in n and k != key and waiting(k):
            changes[k] = _sorted((set(n) - {key}) | set(needs[key]), pos)
    cur = {**needs, **changes}
    before_k = next((k for k in order[pos[key] + 1:] if waiting(k)), None)
    after_k = next((k for k in reversed(order[:pos[key]]) if waiting(k)), None)
    # 2. put it back into the dependency chain at its new place
    if up and before_k is not None:      # runs just before the step now below it
        changes[key] = list(cur[before_k])
        changes[before_k] = [key]
    elif after_k is not None:            # runs just after the step now above it
        changes[key] = [after_k]
        for k, n in cur.items():
            if k not in (key, after_k) and waiting(k) and after_k in n and pos[k] > pos[key]:
                changes[k] = _sorted((set(n) - {after_k}) | {key}, pos)
    else:
        changes[key] = []
    return order, _checked(steps, {k: v for k, v in changes.items() if v != needs.get(k)})
