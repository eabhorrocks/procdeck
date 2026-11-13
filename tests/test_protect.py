"""The daemon and running steps must not be flagged as leftovers or caught in a tree kill/suspend/steer."""
import pytest

from procdeck.control import Controller, Refused
from procdeck.sampler import Row, Sampler, is_orphan

DAEMON, LAUNCHER, STEP, HELPER, TERM = 101, 201, 202, 301, 51


def _row(pid, ppid, name, ctime, kind="app"):
    return Row(pid=pid, ppid=ppid, name=name, ctime=ctime, own=True, kind=kind)


def _rows():
    # terminal -> daemon -> {launcher -> step python, helper python}; daemon's own launcher parent has exited
    return {
        TERM: _row(TERM, 1, "pwsh.exe", 0),
        DAEMON: _row(DAEMON, TERM, "pythonw.exe", 1, kind="daemon"),
        LAUNCHER: _row(LAUNCHER, DAEMON, "pythonw.exe", 2, kind="launcher"),
        STEP: _row(STEP, LAUNCHER, "python.exe", 3),
        HELPER: _row(HELPER, DAEMON, "python.exe", 4),
    }


class FakeRunner:
    managed = {LAUNCHER: {"run": "movie", "step": "fit", "run_id": 1}}


def _controller():
    s = Sampler.__new__(Sampler)  # skip __init__: no live sampling
    s.rows, s.self_pid = _rows(), DAEMON
    return Controller(s, FakeRunner())


def test_daemon_and_launcher_never_orphans():
    rows = _rows()
    del rows[TERM]  # parent of the daemon gone (normal after `procdeck start`)
    watch = ["python.exe", "pythonw.exe"]
    assert not is_orphan(rows, rows[DAEMON], watch)
    rows.pop(DAEMON)  # even if the daemon died, a launcher is not a leftover
    assert not is_orphan(rows, rows[LAUNCHER], watch)
    assert is_orphan(rows, rows[HELPER], watch)  # ordinary python whose parent exited still is


def test_tree_skips_daemon_and_steps():
    c = _controller()
    assert sorted(c.tree(DAEMON)) == [DAEMON, HELPER]        # launcher + step left alone
    assert sorted(c.tree(TERM)) == [TERM]                    # killing the terminal spares the daemon
    assert sorted(c.tree(LAUNCHER)) == [LAUNCHER, STEP]      # a step's own tree is still whole


def test_refuse_kill_or_suspend_daemon():
    c = _controller()
    for action in ("kill", "suspend"):
        with pytest.raises(Refused):
            c.signal(DAEMON, action)
