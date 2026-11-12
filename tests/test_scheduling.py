from procdeck.runner import admit, deps_state, simulate


def ctx(**kw):
    c = {"avail_gb": 10.0, "total_gb": 16.0, "headroom_gb": 1.5, "outstanding_gb": 0.0, "threads_budget": 12,
         "max_running": None, "running": 0, "threads_running": 0, "run_running": {}, "step_running": {}}
    c.update(kw)
    return c


def cand(**kw):
    c = {"run_id": "r", "name": "s", "key": "s", "threads": 6, "mem_gb": 4.0, "run_max_parallel": None,
         "step_max_parallel": None, "needs": [], "state": "waiting"}
    c.update(kw)
    return c


def test_admit_ram():
    assert admit(cand(mem_gb=4), ctx())[0]
    ok, why = admit(cand(mem_gb=9), ctx(running=1, threads_running=1))
    assert not ok and "RAM" in why
    # outstanding growth of running steps counts against free RAM
    ok, _ = admit(cand(mem_gb=4), ctx(running=1, threads_running=1, outstanding_gb=5))
    assert not ok


def test_admit_never_fits_runs_alone():
    assert admit(cand(mem_gb=40), ctx(running=0))[0]
    assert not admit(cand(mem_gb=40), ctx(running=1, threads_running=1))[0]


def test_admit_threads_and_parallel():
    assert not admit(cand(threads=8), ctx(running=1, threads_running=6))[0]
    assert admit(cand(threads=8), ctx(running=0))[0]  # one step may always run
    assert not admit(cand(run_max_parallel=2), ctx(run_running={"r": 2}))[0]
    assert not admit(cand(step_max_parallel=1), ctx(step_running={("r", "s"): 1}))[0]
    assert not admit(cand(), ctx(max_running=1, running=1, threads_running=1))[0]


def test_deps_state():
    by = {"a": {"state": "done", "on_fail": "stop"}, "b": {"state": "running", "on_fail": "stop"},
          "c": {"state": "failed", "on_fail": "stop"}, "d": {"state": "failed", "on_fail": "continue"}}
    assert deps_state({"needs": ["a"]}, by) == "ready"
    assert deps_state({"needs": ["a", "b"]}, by) == "wait"
    assert deps_state({"needs": ["c"]}, by) == "blocked"
    assert deps_state({"needs": ["d", "a"]}, by) == "ready"


def test_simulate_respects_max_parallel_and_deps():
    steps = [cand(key=f"sort[{i}]", name="sort", seq=i, run_max_parallel=2, mem_gb=1, threads=2) for i in range(3)]
    steps.append(cand(key="summary", name="summary", seq=3, needs=[s["key"] for s in steps], run_max_parallel=2,
                      mem_gb=1, threads=2))
    proj = simulate(steps, 0.0, ctx(), {s["key"]: 100 for s in steps})
    assert proj["sort[0]"] == (0.0, 100.0)
    assert proj["sort[1]"] == (0.0, 100.0)
    assert proj["sort[2]"] == (100.0, 200.0)
    assert proj["summary"] == (200.0, 300.0)


def test_simulate_ram_limits_concurrency():
    steps = [cand(key=f"j{i}", name="j", seq=i, mem_gb=4, threads=1) for i in range(3)]
    # 10 GB avail - 1.5 headroom = 8.5 -> two 4 GB jobs fit at once
    proj = simulate(steps, 0.0, ctx(), {s["key"]: 50 for s in steps})
    assert [proj[f"j{i}"][0] for i in range(3)] == [0.0, 0.0, 50.0]
