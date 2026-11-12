"""Memory-pressure alerts with growth prediction."""
from __future__ import annotations

import time
from collections import deque

GB = 1024 ** 3


def linear_slope(points: list[tuple[float, float]]) -> float:
    """Least-squares slope (units of y per unit of x). 0 if fewer than 3 points or no spread in x."""
    n = len(points)
    if n < 3:
        return 0.0
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points)
    if sxx <= 0:
        return 0.0
    return sum((p[0] - mx) * (p[1] - my) for p in points) / sxx


def predict_full_s(avail_bytes: float, slope_bytes_per_s: float) -> float | None:
    """Seconds until available RAM reaches zero at the current growth rate (None if not growing)."""
    if slope_bytes_per_s <= 1e5:  # < ~6 MB/min is noise
        return None
    return max(0.0, avail_bytes / slope_bytes_per_s)


def level_for(ram_pct: float, commit_pct: float, eta_s: float | None, cfg: dict) -> str:
    if ram_pct >= cfg["ram_critical"]:
        return "critical"
    if ram_pct >= cfg["ram_warn"] or commit_pct >= cfg["commit_warn"]:
        return "warn"
    if eta_s is not None and eta_s < cfg["eta_warn_minutes"] * 60 and ram_pct >= 60:
        return "warn"
    return "ok"


class AlertEngine:
    WINDOW_S = 60

    def __init__(self, cfg: dict, notify=None):
        self.cfg = cfg
        self.notify = notify or (lambda title, msg: None)
        self.sys_pts: deque = deque()
        self.group_pts: dict[str, deque] = {}
        self.level = "ok"
        self.last_toast: dict[str, float] = {}
        self.state: dict = {"level": "ok"}

    def update_cfg(self, cfg: dict) -> None:
        self.cfg = cfg

    def update(self, snap: dict) -> dict:
        now = snap["t"]
        sy = snap["system"]
        cut = now - self.WINDOW_S
        self.sys_pts.append((now, float(sy["ram_used"])))
        while self.sys_pts and self.sys_pts[0][0] < cut:
            self.sys_pts.popleft()
        slope = linear_slope(list(self.sys_pts))
        eta = predict_full_s(sy["ram_avail"], slope)

        # fastest-growing group
        seen = set()
        best = None
        for g in snap["groups"]:
            if g["kind"] == "system":
                continue
            gid = g["id"]
            seen.add(gid)
            d = self.group_pts.setdefault(gid, deque())
            d.append((now, float(g["rss"])))
            while d and d[0][0] < cut:
                d.popleft()
            if len(d) >= 10:
                s = linear_slope(list(d))
                if s > 0 and (best is None or s > best[1]):
                    best = (g, s)
        for gid in [k for k in self.group_pts if k not in seen]:
            del self.group_pts[gid]

        level = level_for(sy["ram_pct"], sy["commit_pct"], eta, self.cfg)
        grower = None
        if best and best[1] * 60 > 20 * 1024 ** 2:  # > 20 MB/min
            grower = {"id": best[0]["id"], "label": best[0]["label"], "pid": best[0]["pid"],
                      "mb_per_min": best[1] * 60 / 1024 ** 2}
        self.state = {
            "level": level, "ram_pct": sy["ram_pct"], "commit_pct": sy["commit_pct"],
            "growth_mb_per_min": slope * 60 / 1024 ** 2, "eta_s": eta, "grower": grower,
            "actions": list(self.cfg.get("on_critical") or []) if level == "critical" else [],
        }
        self._maybe_notify(level, now)
        self.level = level
        return self.state

    def _maybe_notify(self, level: str, now: float) -> None:
        if not self.cfg.get("toast", True):
            return
        rank = {"ok": 0, "warn": 1, "critical": 2}
        if rank[level] <= rank[self.level]:
            return
        if now - self.last_toast.get(level, 0) < 300:
            return
        self.last_toast[level] = now
        st = self.state
        msg = f"RAM {st['ram_pct']:.0f}%"
        if st["eta_s"] is not None:
            msg += f", full in ~{max(1, round(st['eta_s'] / 60))} min"
        if st["grower"]:
            msg += f". Growing fastest: {st['grower']['label']} (+{st['grower']['mb_per_min']:.0f} MB/min)"
        self.notify("procdeck: memory " + ("CRITICAL" if level == "critical" else "warning"), msg)
