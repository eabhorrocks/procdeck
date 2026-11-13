import pytest

from procdeck.replan import has_cycle, plan_move, plan_needs, plan_parallel
from procdeck.runner import Runner
from procdeck.store import Store


def steps(spec, states=None):
    """spec: {key: [needs]} in queue order."""
    states = states or {}
    return [{"key": k, "state": states.get(k, "waiting"), "needs": list(n)} for k, n in spec.items()]


def apply(ss, order, changes):
    by = {s["key"]: s for s in ss}
    return [{**by[k], "needs": changes.get(k, by[k]["needs"])} for k in order]


def needs_of(ss):
    return {s["key"]: s["needs"] for s in ss}


CHAIN = {"a": [], "b": ["a"], "c": ["b"], "d": ["c"]}


def test_move_up_a_chain_keeps_it_a_chain():
    ss = steps(CHAIN)
    order, ch = plan_move(ss, "c", "a")
    assert order == ["c", "a", "b", "d"]
    assert needs_of(apply(ss, order, ch)) == {"c": [], "a": ["c"], "b": ["a"], "d": ["b"]}


def test_move_down_a_chain():
    ss = steps(CHAIN)
    order, ch = plan_move(ss, "b", None)           # last
    assert order == ["a", "c", "d", "b"]
    assert needs_of(apply(ss, order, ch)) == {"a": [], "c": ["a"], "d": ["c"], "b": ["d"]}


def test_move_middle_of_chain():
    ss = steps(CHAIN)
    order, ch = plan_move(ss, "b", "d")            # a, c, b, d
    assert order == ["a", "c", "b", "d"]
    assert needs_of(apply(ss, order, ch)) == {"a": [], "c": ["a"], "b": ["c"], "d": ["b"]}


def test_move_in_parallel_run_only_changes_queue_order():
    ss = steps({"a": [], "b": [], "c": [], "s": ["a", "b", "c"]})
    order, ch = plan_move(ss, "c", "a")
    assert order == ["c", "a", "b", "s"] and ch == {}


def test_move_without_rewire_leaves_needs_alone():
    order, ch = plan_move(steps(CHAIN), "c", "a", rewire=False)
    assert order == ["c", "a", "b", "d"] and ch == {}


def test_move_ignores_finished_dependencies():
    ss = steps(CHAIN, {"a": "done"})
    order, ch = plan_move(ss, "c", "b")            # crosses b (a dependency): chain stays valid
    assert order == ["a", "c", "b", "d"]
    new = needs_of(apply(ss, order, ch))
    assert new["c"] == ["a"] and new["b"] == ["c"] and new["d"] == ["b"]
    assert not has_cycle(new)


def test_only_waiting_steps_move():
    ss = steps(CHAIN, {"a": "done", "b": "running"})
    with pytest.raises(ValueError):
        plan_move(ss, "b", "c")
    with pytest.raises(ValueError):
        plan_move(ss, "c", "a")                    # cannot drop above a finished step
    with pytest.raises(ValueError):
        plan_move(ss, "zzz", None)


def test_set_needs_validates():
    ss = steps(CHAIN)
    assert plan_needs(ss, "d", []) == {"d": []}
    assert plan_needs(ss, "d", ["b", "a"]) == {"d": ["a", "b"]}   # kept in queue order
    with pytest.raises(ValueError):
        plan_needs(ss, "a", ["d"])                 # circle
    with pytest.raises(ValueError):
        plan_needs(ss, "a", ["a"])
    with pytest.raises(ValueError):
        plan_needs(ss, "a", ["nope"])


def test_parallel_group_in_a_chain():
    ss = steps(CHAIN)
    ch = plan_parallel(ss, ["b", "c"])
    assert needs_of(apply(ss, [s["key"] for s in ss], ch)) == {"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]}


def test_parallel_non_adjacent_steps_does_not_create_a_cycle():
    ss = steps(CHAIN)
    new = needs_of(apply(ss, [s["key"] for s in ss], plan_parallel(ss, ["b", "d"])))
    assert new["b"] == ["a"] and new["d"] == ["a"] and new["c"] == ["b", "d"]
    assert not has_cycle(new)


def test_parallel_single_step_is_released():
    assert plan_parallel(steps(CHAIN), ["d"]) == {"d": []}


def test_parallel_needs_waiting_steps():
    with pytest.raises(ValueError):
        plan_parallel(steps(CHAIN, {"b": "running"}), ["b", "c"])


# ---- through the runner / database
@pytest.fixture
def runner(tmp_path):
    return Runner(Store(tmp_path / "p.db"), {}, {}, sampler=None)


def test_runner_persists_order_and_needs(runner):
    st = runner.store
    st.add_run({"id": "r1", "name": "r", "submitted_at": 0.0},
               [{"key": k, "name": k, "seq": i, "needs": n} for i, (k, n) in enumerate(CHAIN.items())])
    runner.move_step("r1", "c", "a")
    got = {s["key"]: (s["seq"], s["needs"]) for s in st.steps("r1")}
    assert [k for k, _ in sorted(got.items(), key=lambda kv: kv[1][0])] == ["c", "a", "b", "d"]
    assert got["a"][1] == ["c"] and got["c"][1] == []
    runner.make_parallel("r1", ["a", "b"])
    assert {s["key"]: s["needs"] for s in st.steps("r1")}["b"] == ["c"]
    runner.set_max_parallel("r1", 4)
    assert st.run("r1")["max_parallel"] == 4
    runner.set_max_parallel("r1", 0)
    assert st.run("r1")["max_parallel"] is None
    with pytest.raises(ValueError):
        runner.set_needs("r1", "c", ["d"])        # c -> ... -> d would be a circle
