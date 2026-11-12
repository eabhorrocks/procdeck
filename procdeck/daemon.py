"""The long-running daemon: sampling loop + scheduler + rules, shared with the HTTP API."""
from __future__ import annotations

import logging
import threading
import time

import psutil

from . import __version__, win32
from .alerts import AlertEngine
from .config import CONFIG_FILES, DB_PATH, config_path, ensure_dirs, load_profiles, load_rules, load_settings
from .control import Controller, RuleEngine
from .runner import Runner
from .sampler import Sampler
from .store import Store

log = logging.getLogger("procdeck")


class Daemon:
    def __init__(self):
        ensure_dirs()
        self.settings = load_settings()
        self.profiles = load_profiles()
        self.store = Store(DB_PATH)
        self.sampler = Sampler(self.settings)
        self.alerts = AlertEngine(self.settings["alerts"], notify=self.notify)
        self.runner = Runner(self.store, self.settings, self.profiles, self.sampler, notify=self.notify)
        self.ctl = Controller(self.sampler, self.runner)
        self.rules = RuleEngine(self.ctl, load_rules())
        self.op_lock = threading.RLock()     # serialises scheduler ticks with API actions
        self.snap: dict | None = None
        self.alert: dict = {"level": "ok"}
        self.runs_cache: list[dict] = []
        self._runs_t = 0.0
        self._hist_t = 0.0
        self._prune_t = 0.0
        self.started = time.time()
        self._cfg_t = 0.0
        self._cfg_mtimes = self._config_mtimes()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_error: str | None = None

    def notify(self, title: str, msg: str) -> None:
        if self.settings["alerts"].get("toast", True):
            win32.toast(title, msg)

    @staticmethod
    def _config_mtimes() -> tuple:
        out = []
        for n in CONFIG_FILES:
            p = config_path(n)
            out.append(p.stat().st_mtime if p.exists() else 0)
        return tuple(out)

    def reload_config(self) -> None:
        with self.op_lock:
            self.settings = load_settings()
            self.profiles = load_profiles()
            self.sampler.update_settings(self.settings)
            self.alerts.update_cfg(self.settings["alerts"])
            self.runner.update_config(self.settings, self.profiles)
            self.rules.update_rules(load_rules())
        log.info("config reloaded")

    # ---------------------------------------------------------------- loop
    def start(self) -> None:
        try:
            psutil.Process().nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS)  # stay responsive when the machine is loaded
        except psutil.Error:
            pass
        self.thread = threading.Thread(target=self._loop, name="procdeck-loop", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            t0 = time.time()
            try:
                self.tick()
                self.last_error = None
            except Exception as e:  # keep the loop alive no matter what
                log.exception("tick failed")
                self.last_error = f"{type(e).__name__}: {e}"
            interval = float(self.settings.get("sample_interval", 1.0))
            self.stop_event.wait(max(0.05, interval - (time.time() - t0)))

    def tick(self) -> None:
        snap = self.sampler.tick(self.runner.managed)
        alert = self.alerts.update(snap)
        with self.op_lock:
            self.runner.tick(snap, alert)
            self.rules.tick()
        now = snap["t"]
        if now - self._runs_t >= 2:
            self.runs_cache = self.runner.runs_view(30)
            self._runs_t = now
        if snap.get("warm") and now - self._hist_t >= float(self.settings.get("history_interval", 5)):
            keep = [g for g in snap["groups"] if g["kind"] in ("job", "managed") and g["rss"] > 50 * 1024 ** 2]
            keep += [g for g in snap["groups"][:10] if g not in keep]
            self.store.add_samples(now, snap["system"], keep)
            self._hist_t = now
        if now - self._cfg_t >= 5:
            self._cfg_t = now
            mt = self._config_mtimes()
            if mt != self._cfg_mtimes:
                self._cfg_mtimes = mt
                try:
                    self.reload_config()
                except Exception as e:  # bad YAML: keep the old config, surface the error
                    log.warning("config reload failed: %s", e)
                    self.last_error = f"config: {e}"
        if now - self._prune_t >= 3600:
            self.store.prune(float(self.settings.get("history_days", 30)))
            self._prune_t = now
        self.snap, self.alert = snap, alert

    def state(self) -> dict:
        snap = self.snap or {"t": time.time(), "system": {}, "groups": [], "daemon_cpu": 0, "tick_ms": 0}
        hist = list(self.sampler.sys_hist)[::2]
        return {
            "t": snap["t"], "system": snap["system"], "groups": snap["groups"], "alert": self.alert,
            "admission": self.runner.admission_info, "runs": self.runs_cache,
            "sys_hist": [[round(t), round(c, 1), round(r, 1)] for t, c, r in hist],
            "daemon": {"cpu": snap.get("daemon_cpu", 0), "tick_ms": snap.get("tick_ms", 0), "version": __version__,
                       "started": self.started, "error": self.last_error,
                       "interval": self.settings.get("sample_interval", 1.0)},
            "profiles": self.profiles,
        }
