import sqlite3

import pytest

from procdeck.runner import Runner
from procdeck.store import Store


def add(store, rid, state, step_state="done", t=0.0):
    store.add_run({"id": rid, "name": rid.split("-")[0], "submitted_at": t}, [
        {"key": "a", "name": "a", "seq": 0, "script_key": "a.py", "started_at": 0, "ended_at": 60, "peak_rss": 1e9}])
    store.update_run(rid, state=state)
    store.update_step(rid, "a", state=step_state, started_at=0.0, ended_at=60.0)


@pytest.fixture
def runner(tmp_path):
    store = Store(tmp_path / "p.db")
    return Runner(store, {}, {}, sampler=None)


def test_forget_hides_but_keeps_history(runner):
    s = runner.store
    add(s, "grey-1", "cancelled", "done", 1)
    add(s, "grey-2", "running", "running", 2)
    runner.forget(["grey-1"])
    assert [r["id"] for r in s.runs()] == ["grey-2"]
    assert {r["id"] for r in s.runs(include_hidden=True)} == {"grey-1", "grey-2"}
    assert s.estimate("a.py")["n"] == 1          # the forgotten run's done step still feeds estimates
    assert s.find_run("grey-1")["id"] == "grey-1"  # still reachable by id
    with pytest.raises(ValueError):
        runner.forget(["grey-2"])                 # active runs must be cancelled first


def test_find_by_name_prefers_visible(runner):
    s = runner.store
    add(s, "20261112-1", "cancelled", t=1)
    add(s, "20261112-2", "done", t=2)
    s.update_run("20261112-1", name="q35")
    s.update_run("20261112-2", name="q35")
    runner.forget(["20261112-2"])
    assert s.find_run("q35")["id"] == "20261112-1"


def test_clear_by_state_and_retry_unhides(runner):
    s = runner.store
    add(s, "a-1", "cancelled", "cancelled", 1)
    add(s, "b-1", "failed", "failed", 2)
    add(s, "c-1", "done", "done", 3)
    add(s, "d-1", "queued", "waiting", 4)
    assert runner.clear(["cancelled", "queued"]) == ["a-1"]   # never clears active runs
    assert {r["id"] for r in s.runs()} == {"b-1", "c-1", "d-1"}
    runner.clear(["done", "failed", "cancelled"])
    assert [r["id"] for r in s.runs()] == ["d-1"]
    assert runner.retry("b-1") == 1
    assert {r["id"] for r in s.runs()} == {"b-1", "d-1"}


def test_migrates_old_database(tmp_path):
    p = tmp_path / "old.db"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, name TEXT, description TEXT, spec_path TEXT, spec_text TEXT, "
               "submitted_at REAL, submitted_by TEXT, priority INTEGER, max_parallel INTEGER, state TEXT, "
               "paused INTEGER DEFAULT 0, finished_at REAL)")
    db.execute("INSERT INTO runs (id, name, state, submitted_at) VALUES ('x', 'x', 'done', 1)")
    db.commit()
    db.close()
    s = Store(p)
    assert s.runs()[0]["hidden"] == 0
