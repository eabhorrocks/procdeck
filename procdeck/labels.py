"""Human-readable labels for processes, derived from command line + working directory. Pure functions."""
from __future__ import annotations

import ntpath
import re

FRIENDLY = {
    "chrome.exe": "Chrome", "msedge.exe": "Edge", "firefox.exe": "Firefox", "code.exe": "VS Code",
    "ms-teams.exe": "Teams", "teams.exe": "Teams", "explorer.exe": "Explorer", "claude.exe": "Claude",
    "outlook.exe": "Outlook", "olk.exe": "Outlook", "winword.exe": "Word", "excel.exe": "Excel",
    "powerpnt.exe": "PowerPoint", "onedrive.exe": "OneDrive", "obsidian.exe": "Obsidian",
    "slack.exe": "Slack", "zoom.exe": "Zoom", "spotify.exe": "Spotify", "windowsterminal.exe": "Terminal",
    "matlab.exe": "MATLAB", "python.exe": "python", "pythonw.exe": "pythonw",
}

PY_OPTS_WITH_ARG = {"-X", "-W", "-Q"}
PY_FLAGS = set("-u -B -O -OO -I -E -s -S -q -v -b -bb -d -i -R".split())


def friendly_name(name: str) -> str:
    n = (name or "").lower()
    if n in FRIENDLY:
        return FRIENDLY[n]
    return name[:-4] if name.lower().endswith(".exe") else (name or "?")


def _project_of(path: str | None, cwd: str | None) -> str:
    """Folder name that best identifies the project."""
    for p in (ntpath.dirname(path) if path and ntpath.isabs(path) else None, cwd):
        if not p:
            continue
        base = ntpath.basename(p.rstrip("\\/"))
        low = p.lower()
        if base and "system32" not in low and base.lower() not in ("scripts", "bin", "src", "users"):
            return base
        if base.lower() in ("scripts", "bin", "src"):
            parent = ntpath.basename(ntpath.dirname(p.rstrip("\\/")))
            if parent:
                return parent
    return ""


def _short(s: str, n: int = 48) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def python_label(cmdline: list[str], cwd: str | None) -> tuple[str, str, str]:
    """-> (kind, label, script_key). kind: script | module | worker | kernel | inline | interpreter"""
    joined = " ".join(cmdline)
    low = joined.lower()
    if "loky" in low:
        return "worker", "loky worker", ""
    if "multiprocessing" in low and ("spawn_main" in low or "--multiprocessing-fork" in low):
        return "worker", "multiprocessing worker", ""
    args = cmdline[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a in PY_OPTS_WITH_ARG:
            i += 2
            continue
        if a in PY_FLAGS or (a.startswith("-X") or a.startswith("-W")):
            i += 1
            continue
        break
    rest = args[i:]
    if not rest:
        return "interpreter", "python (interactive)", ""
    if rest[0] == "-m" and len(rest) > 1:
        mod = rest[1]
        if mod.startswith("ipykernel"):
            proj = _project_of(None, cwd)
            return "kernel", f"{proj} › Jupyter kernel" if proj else "Jupyter kernel", ""
        proj = _project_of(None, cwd)
        extra = _short(" ".join(rest[2:]), 40)
        lab = f"-m {mod}" + (f" {extra}" if extra else "")
        return "module", f"{proj} › {lab}" if proj else lab, f"{proj}/-m {mod}"
    if rest[0] == "-c":
        return "inline", "python -c …", ""
    script = rest[0]
    base = ntpath.basename(script)
    if base.lower() == "conda-script.py" and len(rest) > 1 and rest[1] == "run":
        return conda_run_label(rest[2:], cwd)
    proj = _project_of(script, cwd)
    extra = _short(" ".join(rest[1:]), 40)
    lab = base + (f" {extra}" if extra else "")
    return "script", f"{proj} › {lab}" if proj else lab, f"{proj}/{base}"


def conda_run_label(args: list[str], cwd: str | None) -> tuple[str, str, str]:
    """`conda run -n ENV [--no-capture-output] [--cwd X] python script.py ...` -> label of the inner command."""
    env, i = "", 0
    while i < len(args):
        a = args[i]
        if a in ("-n", "--name", "-p", "--prefix", "--cwd") and i + 1 < len(args):
            if a in ("-n", "--name", "-p", "--prefix"):
                env = ntpath.basename(args[i + 1].rstrip("\\/"))
            else:
                cwd = args[i + 1]
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        break
    inner = args[i:]
    if not inner:
        return "script", f"conda run [{env}]", ""
    prog = ntpath.basename(inner[0]).lower()
    if not prog.endswith(".exe"):
        prog += ".exe"
    kind, label, key = describe(prog, [prog] + inner[1:], cwd)
    return kind, f"{label} [{env}]" if env else label, key


_MATLAB_ARG = re.compile(r"-(batch|r)\s*$", re.I)


def matlab_label(cmdline: list[str], cwd: str | None) -> tuple[str, str, str]:
    low = " ".join(cmdline).lower()
    if "dmlworker" in low or "-parallelworker" in low or "mdce" in low or "parallel_worker" in low:
        return "worker", "MATLAB worker", ""
    for i, a in enumerate(cmdline[:-1]):
        if a.lower() in ("-batch", "-r"):
            code = cmdline[i + 1]
            fn = re.match(r"\s*([A-Za-z]\w*)", code)
            proj = _project_of(None, cwd)
            lab = f"MATLAB {a.lower()} {_short(code, 40)}"
            return "script", f"{proj} › {lab}" if proj else lab, f"{proj}/matlab:{fn.group(1) if fn else code[:20]}"
    if "-batch" in low or " -r " in low:
        return "script", "MATLAB batch", ""
    return "desktop", "MATLAB (desktop)", ""


def describe(name: str, cmdline: list[str] | None, cwd: str | None) -> tuple[str, str, str]:
    """-> (kind, label, script_key) for any process."""
    n = (name or "").lower()
    cmdline = cmdline or []
    if n in ("python.exe", "pythonw.exe") and cmdline:
        return python_label(cmdline, cwd)
    if n == "matlab.exe" and cmdline:
        return matlab_label(cmdline, cwd)
    return "app", friendly_name(name), ""


def split_first_token(cmd: str) -> tuple[str, str]:
    """Split a Windows command line into (program, rest), honouring a leading quoted program path."""
    cmd = cmd.lstrip()
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        if end == -1:
            return cmd[1:], ""
        return cmd[1:end], cmd[end + 1:].lstrip()
    parts = cmd.split(None, 1)
    return (parts[0], parts[1] if len(parts) > 1 else "") if parts else ("", "")


def script_key_from_cmd(cmd: str, cwd: str | None) -> str:
    """History key for a pipeline step command (same scheme as describe())."""
    import shlex
    try:
        toks = shlex.split(cmd, posix=False)
    except ValueError:
        toks = cmd.split()
    toks = [t.strip('"') for t in toks]
    if not toks:
        return ""
    prog = ntpath.basename(toks[0]).lower()
    if not prog.endswith(".exe"):
        prog += ".exe"
    return describe(prog, [prog] + toks[1:], cwd)[2]


def inject_matlab_threads(cmd: str, n: int) -> str:
    """Prepend maxNumCompThreads(n); to the code of a -batch "..." or -r "..." argument."""
    return re.sub(r'(-(?:batch|r)\s+")', lambda m: f"{m.group(1)}maxNumCompThreads({n}); ", cmd, count=1,
                  flags=re.I)
