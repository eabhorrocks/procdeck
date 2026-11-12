"""procdeck command line. Every command except `serve` talks to the daemon over HTTP (starting it if needed)."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import win32
from .config import DAEMON_LOG, REPO_DIR, TOKEN_PATH, ensure_dirs, get_token, load_settings

GB = 1024 ** 3
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000


# ------------------------------------------------------------------------------------------- http
class DaemonDown(Exception):
    pass


class ApiError(Exception):
    def __init__(self, code: int, detail):
        self.code, self.detail = code, detail
        super().__init__(f"error {code}: {detail}")


def port() -> int:
    return int(load_settings()["port"])


def base_url() -> str:
    return f"http://127.0.0.1:{port()}"


# never route localhost through an HTTP proxy from the environment or registry
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(method: str, path: str, body: dict | None = None, timeout: float = 30, autostart: bool = True):
    url = base_url() + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"x-procdeck-token": get_token(), "content-type": "application/json"})
    try:
        with _opener.open(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read())
        except ValueError:
            payload = {"detail": str(e)}
        if e.code == 422 and isinstance(payload, dict) and "errors" in payload:
            return payload
        raise ApiError(e.code, payload.get("detail", payload) if isinstance(payload, dict) else payload)
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        if autostart:
            start_daemon(quiet=True)
            return api(method, path, body, timeout, autostart=False)
        raise DaemonDown(str(e))


def port_listening() -> bool:
    import socket
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", port()))
        return True
    except OSError:
        return False
    finally:
        s.close()


def daemon_state() -> str:
    """'up' | 'forbidden' (a daemon answers but rejects our token) | 'unresponsive' | 'down'."""
    try:
        api("GET", "/api/top?n=1", timeout=10, autostart=False)
        return "up"
    except ApiError as e:
        return "forbidden" if e.code == 403 else "unresponsive"
    except DaemonDown:
        return "unresponsive" if port_listening() else "down"


def daemon_up() -> bool:
    return daemon_state() == "up"


def _token_mismatch_msg() -> str:
    return (f"A procdeck daemon is running on port {port()} but rejected the token in {TOKEN_PATH}. "
            f"It was probably started with a different data folder (older "
            f"procdeck version, or from a shell whose AppData is redirected). Stop it - end its pythonw.exe "
            f"('procdeck serve') in Task Manager or with `procdeck stop` from where it was started - then run "
            f"`procdeck start` again.")


def _spawn_outside(argv: list[str]) -> str:
    """Start argv detached, outside this shell's job object and outside any AppData redirection."""
    flags = CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW
    if not win32.appdata_redirected():
        try:
            subprocess.Popen(argv, cwd=str(REPO_DIR), creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, close_fds=True)
            return "breakaway"
        except OSError:
            pass  # job forbids breakaway: it would kill the daemon when this shell closes
    # Processes created through WMI are parented by the WMI service: outside our job and our app container.
    cmdline = subprocess.list2cmdline(argv).replace("'", "''")
    cwd = str(REPO_DIR).replace("'", "''")
    ps = ("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
          f"@{{CommandLine='{cmdline}'; CurrentDirectory='{cwd}'}}; exit $r.ReturnValue")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       creationflags=CREATE_NO_WINDOW, check=False, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"could not start {Path(argv[0]).name} via WMI (code {r.returncode}): {r.stderr.strip()[:300]}")
    return "wmi"


def find_app_browser() -> str | None:
    """Edge (preferred) or Chrome, for opening the dashboard as a standalone app window."""
    cands = []
    try:
        import winreg
        for exe in ("msedge.exe", "chrome.exe"):
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                        cands.append(winreg.QueryValue(k, None))
                except OSError:
                    pass
    except ImportError:
        pass
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            cands += [str(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
                      str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")]
    cands.sort(key=lambda c: "chrome" in c.lower())  # Edge first
    return next((c for c in cands if c and os.path.exists(c)), None)


def start_daemon(quiet: bool = False) -> None:
    state = daemon_state()
    if state == "up":
        if not quiet:
            print(f"procdeck daemon already running: {dashboard_url()}")
        return
    if state == "forbidden":
        raise SystemExit(_token_mismatch_msg())
    if state == "unresponsive":
        raise SystemExit(f"Something is listening on port {port()} but not answering procdeck requests. "
                         f"If it is a stuck procdeck daemon, end its pythonw.exe; otherwise change `port` in "
                         f"{REPO_DIR / 'config' / 'settings.yaml'}.")
    pyw = str(Path(sys.executable).with_name("pythonw.exe"))
    if not os.path.exists(pyw):
        pyw = sys.executable
    how = _spawn_outside([pyw, "-m", "procdeck", "serve"])
    for _ in range(80):
        time.sleep(0.25)
        if daemon_up():
            if not quiet:
                print(f"procdeck daemon started ({how}): {dashboard_url()}")
            return
    raise SystemExit(f"daemon did not start within 20 s; see {DAEMON_LOG}")


def dashboard_url() -> str:
    return f"{base_url()}/?token={urllib.parse.quote(get_token())}"


# ------------------------------------------------------------------------------------------- formatting
def gb(b) -> str:
    return f"{(b or 0) / GB:.1f} GB"


def dur(s) -> str:
    if s is None:
        return "-"
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def hm(ts) -> str:
    if not ts:
        return "-"
    t = dt.datetime.fromtimestamp(ts)
    return t.strftime("%H:%M") if t.date() == dt.date.today() else t.strftime("%a %d %b %H:%M")


def table(rows: list[list], headers: list[str]) -> str:
    rows = [[str(c) for c in r] for r in rows]
    w = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    line = lambda r: "  ".join(c.ljust(w[i]) for i, c in enumerate(r)).rstrip()  # noqa: E731
    return "\n".join([line(headers), line(["-" * x for x in w])] + [line(r) for r in rows])


def out_json(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


# ------------------------------------------------------------------------------------------- commands
def cmd_serve(a) -> None:
    import uvicorn
    from .daemon import Daemon
    from .server import create_app

    ensure_dirs()
    handlers = [logging.FileHandler(DAEMON_LOG, encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    else:  # pythonw: no console
        sys.stdout = sys.stderr = open(DAEMON_LOG, "a", encoding="utf-8", buffering=1)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    if win32.appdata_redirected():
        logging.getLogger("procdeck").warning(
            "this daemon's AppData writes (and its jobs') are redirected into an app's private folder; "
            "start it with `procdeck start` instead, which launches it outside")
    settings = load_settings()
    port = a.port or settings["port"]
    d = Daemon()
    d.start()
    server = uvicorn.Server(uvicorn.Config(create_app(d, get_token(), port, on_shutdown=lambda: setattr(
        server, "should_exit", True)), host="127.0.0.1", port=port, log_level="warning", log_config=None))
    logging.getLogger("procdeck").info("procdeck daemon on http://127.0.0.1:%d (pid %d)", port, os.getpid())
    server.run()
    d.stop_event.set()


def cmd_start(a) -> None:
    start_daemon()


def cmd_stop(a) -> None:
    state = daemon_state()
    if state == "forbidden":
        raise SystemExit(_token_mismatch_msg())
    if state != "up":
        print("daemon not running" if state == "down" else "daemon not answering; end its pythonw.exe")
        return
    api("POST", "/api/shutdown", {}, autostart=False)
    print("daemon stopping (running steps keep going; they are re-attached on next start)")


def cmd_open(a) -> None:
    start_daemon(quiet=True)
    url = dashboard_url()
    dash = load_settings().get("dashboard") or {}
    mode = "browser" if a.browser else "app" if a.app else dash.get("open_as", "app")
    if mode == "app":
        exe = find_app_browser()
        if exe:
            w, h = (dash.get("window_size") or [1300, 900])[:2]
            _spawn_outside([exe, f"--app={url}", f"--window-size={int(w)},{int(h)}"])
            print(f"opened the dashboard in its own {Path(exe).stem} window")
            return
        print("Edge/Chrome not found; opening your default browser instead")
    if win32.appdata_redirected():
        _spawn_outside(["explorer.exe", url])  # from inside a packaged app, hand the URL to the real shell
    else:
        import webbrowser
        webbrowser.open(url)
    print(f"opened {base_url()} in your default browser")


def cmd_url(a) -> None:
    print(dashboard_url())


def cmd_submit(a) -> None:
    spec = str(Path(a.spec).resolve())
    r = api("POST", "/api/submit", {"spec_path": spec, "dry_run": a.dry_run, "submitted_by": a.by or ""})
    if a.json:
        return out_json(r)
    if not r.get("ok"):
        print("Spec has problems:")
        for e in r.get("errors", []):
            print(f"  - {e}")
        raise SystemExit(2)
    if a.dry_run:
        now = r["now"]
        rows = []
        for s in r["steps"]:
            mem = f"{s['mem_gb']:.1f} GB ({s['mem_source']})"
            h = s.get("history")
            est = dur(h["median_s"]) if h else "? (no history)"
            rows.append([s["key"], ", ".join(s["needs"])[:30] or "-", s["profile"], s["threads"], s["cores"], mem,
                         est, f"+{dur(s['proj_start'] - now)}"])
        print(f"Run '{r['name']}' (dry run, nothing started)  priority {r['priority']}  "
              f"max_parallel {r['max_parallel'] or '-'}")
        print(table(rows, ["step", "needs", "profile", "thr", "cores", "mem", "typical time", "starts"]))
        print()
        for s in r["steps"]:
            print(f"  {s['key']}: {s.get('program')}  [cwd {s['cwd']}]")
        eta = r["eta"]
        unk = r["unknown_durations"]
        print(f"\nEstimated finish: {hm(eta)} (in {dur(eta - now)})" +
              (f" - {unk} step(s) without history assumed 10 min" if unk else ""))
        for w in r["warnings"]:
            print(f"warning: {w}")
        for p in r["problems"]:
            print(f"PROBLEM: {p}")
        if r["problems"]:
            raise SystemExit(2)
        return
    print(f"Submitted run {r['run_id']} ({r['name']}, {r['steps']} steps).  Status: procdeck status {r['run_id']}")
    for w in r.get("warnings", []):
        print(f"warning: {w}")


def cmd_status(a) -> None:
    if a.run:
        r = api("GET", f"/api/runs/{urllib.parse.quote(a.run)}")
        if a.json:
            return out_json(r)
        print(f"{r['name']}  [{r['id']}]  {r['state']}{' (paused)' if r['paused'] else ''}  priority {r['priority']}"
              f"  submitted {hm(r['submitted_at'])}" + (f"  ETA {hm(r.get('eta'))}" if r.get("eta") else ""))
        if r.get("spec_path"):
            print(f"spec: {r['spec_path']}")
        rows = []
        now = time.time()
        for s in r["steps"]:
            el = (s["ended_at"] or now) - s["started_at"] if s["started_at"] else None
            info = s["error"] or s["held_reason"] or ""
            rows.append([s["key"], s["state"], s["attempt"] or "-", dur(el), gb(s["peak_rss"]) if s["peak_rss"] else "-",
                         s["exit_code"] if s["exit_code"] is not None else "-", info[:70]])
        print(table(rows, ["step", "state", "try", "time", "peak RAM", "exit", "info"]))
        return
    runs = api("GET", "/api/runs?limit=" + str(a.limit))
    if a.json:
        return out_json(runs)
    rows = []
    for r in runs:
        c = r["counts"]
        prog = f"{c.get('done', 0)}/{r['n_steps']} done" + "".join(
            f", {c[k]} {k}" for k in ("running", "failed", "skipped", "cancelled") if c.get(k))
        rows.append([r["id"], r["name"], r["state"] + (" (paused)" if r["paused"] else ""), prog,
                     hm(r["submitted_at"]), hm(r.get("eta")) if r["state"] in ("queued", "running") else hm(r["finished_at"])])
    print(table(rows, ["run id", "name", "state", "steps", "submitted", "eta/finished"]) if rows else "no runs yet")


def cmd_logs(a) -> None:
    path = f"/api/runs/{urllib.parse.quote(a.run)}/log?key={urllib.parse.quote(a.step)}&lines={a.lines}"
    r = api("GET", path)
    print(r["text"])
    if a.follow:
        seen = r["text"]
        try:
            while True:
                time.sleep(2)
                t = api("GET", path)["text"]
                if t != seen:
                    old = seen.split("\n")
                    new = t.split("\n")
                    # print lines after the overlap
                    k = 0
                    for k in range(len(new), 0, -1):
                        if new[:k] == old[-k:]:
                            break
                    print("\n".join(new[k:]))
                    seen = t
        except KeyboardInterrupt:
            pass


def _run_action(a, action: str, **extra) -> None:
    r = api("POST", f"/api/runs/{urllib.parse.quote(a.run)}/action", {"action": action, **extra})
    print(json.dumps(r))


def cmd_top(a) -> None:
    r = api("GET", f"/api/top?n={a.n}")
    if a.json:
        return out_json(r)
    sy, al, ad = r["system"], r["alert"], r["admission"]
    print(f"CPU {sy['cpu']:.0f}%   RAM {gb(sy['ram_used'])} / {gb(sy['ram_total'])} ({sy['ram_pct']:.0f}%)   "
          f"commit {sy['commit_pct']:.0f}%   alert: {al['level']}" +
          (f" (full in ~{al['eta_s'] / 60:.0f} min)" if al.get("eta_s") else ""))
    if ad:
        print(f"procdeck: {ad['running']} step(s) running, {ad['threads_running']}/{ad['threads_budget']} threads, "
              f"{ad['free_gb']:.1f} GB free for new steps" + (" - ADMISSION PAUSED" if ad["paused"] or ad["alert_paused"] else ""))
    rows = [[g["pid"], g["kind"], g["label"][:70], f"{g['cpu']:.1f}%", gb(g["rss"]), g["nproc"], g["priority"],
             g["cores"], ",".join(g["flags"])] for g in r["groups"]]
    print(table(rows, ["pid", "kind", "label", "cpu", "ram", "procs", "priority", "cores", "flags"]))


def cmd_steer(a) -> None:
    body: dict = {"tree": not a.no_tree}
    if a.profile:
        body["profile"] = a.profile
    if a.priority:
        body["priority"] = a.priority
    if a.cores:
        body["cores"] = a.cores
    if a.weight is not None:
        body.update(cpu_mode="weight", cpu_value=a.weight)
    if a.cap is not None:
        body.update(cpu_mode="cap", cpu_value=a.cap)
    if a.no_share:
        body["cpu_mode"] = "none"
    if a.mem_cap is not None:
        body["mem_cap_gb"] = a.mem_cap
    out_json(api("POST", f"/api/procs/{a.pid}/steer", body))


def cmd_signal(a, action: str) -> None:
    out_json(api("POST", f"/api/procs/{a.pid}/signal", {"action": action}))


def cmd_history(a) -> None:
    q = f"?pattern={urllib.parse.quote(a.pattern)}" if a.pattern else ""
    rows = api("GET", "/api/scripts" + q)
    if a.json:
        return out_json(rows)
    print(table([[r["script_key"], r["runs"], dur(r["median_s"]), gb(r["peak_rss"]), f"{r['cpu_hours']:.1f}"]
                 for r in rows], ["script", "runs", "median time", "peak RAM", "CPU-hours"]) if rows
          else "no finished procdeck steps yet")


def cmd_admission(a) -> None:
    out_json(api("POST", "/api/admission", {"paused": a.state == "pause"}))


def cmd_install_skill(a) -> None:
    src = REPO_DIR / "skill"
    dst = Path.home() / ".claude" / "skills" / "procdeck"
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.rglob("*"):
        if f.is_file():
            t = dst / f.relative_to(src)
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, t)
    print(f"installed Claude skill to {dst}")


def cmd_autostart(a) -> None:
    if win32.appdata_redirected():
        raise SystemExit("This shell's AppData writes are redirected into an app's private folder (e.g. a terminal "
                         "inside the Claude desktop app), so a Startup shortcut made here would never run. Run "
                         "`procdeck autostart on|off` from a normal PowerShell or cmd window.")
    startup = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    lnk = startup / "procdeck.lnk"
    if a.state == "off":
        if lnk.exists():
            lnk.unlink()
        print("autostart off")
        return
    pyw = str(Path(sys.executable).with_name("pythonw.exe"))
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');$s.TargetPath='{pyw}';"
          f"$s.Arguments='-m procdeck serve';$s.WorkingDirectory='{REPO_DIR}';$s.WindowStyle=7;$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], check=True,
                   creationflags=CREATE_NO_WINDOW)
    print(f"autostart on: {lnk}")


# ------------------------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="procdeck", description="Local job runner, process monitor and resource steering.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the daemon in the foreground")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)
    sub.add_parser("start", help="start the daemon in the background").set_defaults(fn=cmd_start)
    sub.add_parser("stop", help="stop the daemon (running steps continue)").set_defaults(fn=cmd_stop)
    s = sub.add_parser("open", help="open the dashboard (own window by default; see dashboard.open_as in settings)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--app", action="store_true", help="standalone window (Edge/Chrome app mode)")
    g.add_argument("--browser", action="store_true", help="a tab in your default browser")
    s.set_defaults(fn=cmd_open)
    sub.add_parser("url", help="print the dashboard URL (includes the access token)").set_defaults(fn=cmd_url)

    s = sub.add_parser("submit", help="submit a pipeline spec (YAML)")
    s.add_argument("spec")
    s.add_argument("--dry-run", "-n", action="store_true", help="validate and show the plan without starting")
    s.add_argument("--by", help="who submitted (shown in the dashboard), e.g. claude")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_submit)

    s = sub.add_parser("status", help="list runs, or show one run's steps")
    s.add_argument("run", nargs="?", help="run id, id prefix, or name (latest)")
    s.add_argument("--limit", type=int, default=15)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("logs", help="show a step's log")
    s.add_argument("run")
    s.add_argument("step", help="step key, e.g. sort[M01_2026-10-01]")
    s.add_argument("-n", "--lines", type=int, default=60)
    s.add_argument("-f", "--follow", action="store_true")
    s.set_defaults(fn=cmd_logs)

    for name, action, helptext in (("cancel", "cancel", "cancel a run or one step (kills running trees)"),
                                   ("retry", "retry", "retry failed/cancelled/skipped steps"),
                                   ("pause", "pause", "stop starting new steps of a run"),
                                   ("resume", "resume", "resume a paused run")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("run")
        if action in ("cancel", "retry"):
            s.add_argument("step", nargs="?")
            s.set_defaults(fn=lambda a, act=action: _run_action(a, act, key=a.step))
        else:
            s.set_defaults(fn=lambda a, act=action: _run_action(a, act))
    s = sub.add_parser("prio", help="set a run's priority (higher starts first)")
    s.add_argument("run")
    s.add_argument("value", type=int)
    s.set_defaults(fn=lambda a: _run_action(a, "priority", value=a.value))

    s = sub.add_parser("top", help="what is using CPU/RAM right now")
    s.add_argument("-n", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_top)

    s = sub.add_parser("steer", help="change priority/cores/CPU share/memory cap of a process tree")
    s.add_argument("pid", type=int)
    s.add_argument("--profile")
    s.add_argument("--priority", choices=["idle", "below_normal", "normal", "above_normal", "high"])
    s.add_argument("--cores", help='e.g. "0-5", "6-11", "all"')
    g = s.add_mutually_exclusive_group()
    g.add_argument("--weight", type=int, help="relative CPU weight 1-9")
    g.add_argument("--cap", type=float, help="hard CPU cap, %% of the whole machine")
    g.add_argument("--no-share", action="store_true", help="remove CPU weight/cap")
    s.add_argument("--mem-cap", type=float, help="hard memory cap in GB (0 removes); the process fails at the cap")
    s.add_argument("--no-tree", action="store_true", help="only this process, not its children")
    s.set_defaults(fn=cmd_steer)

    for name in ("kill", "suspend", "resume-proc"):
        s = sub.add_parser(name, help=f"{name.replace('-proc', '')} a whole process tree")
        s.add_argument("pid", type=int)
        s.set_defaults(fn=lambda a, n=name: cmd_signal(a, n.replace("-proc", "")))

    s = sub.add_parser("history", help="typical time / peak RAM per script from past runs")
    s.add_argument("pattern", nargs="?")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_history)

    s = sub.add_parser("admission", help="pause/resume starting any queued steps")
    s.add_argument("state", choices=["pause", "resume"])
    s.set_defaults(fn=cmd_admission)

    sub.add_parser("install-skill", help="install the Claude skill into ~/.claude/skills").set_defaults(
        fn=cmd_install_skill)
    s = sub.add_parser("autostart", help="start the daemon at login (Startup folder shortcut)")
    s.add_argument("state", choices=["on", "off"])
    s.set_defaults(fn=cmd_autostart)
    return p


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except DaemonDown as e:
        raise SystemExit(f"procdeck daemon is not reachable ({e}); try `procdeck start`")
    except ApiError as e:
        if e.code == 403:
            raise SystemExit(_token_mismatch_msg())
        raise SystemExit(f"error {e.code}: {e.detail}")
