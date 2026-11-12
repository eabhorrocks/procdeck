import datetime as dt

import pytest

from procdeck.spec import SpecError, parse, substitute

PROFILES = {"default": {}, "background": {"cores": "6-11"}, "heavy": {}}


def p(text, tmp_path, **kw):
    spec = tmp_path / "jobs" / "x.yaml"
    spec.parent.mkdir(exist_ok=True)
    spec.write_text(text)
    return parse(text, str(spec), PROFILES, **kw)


def test_foreach_needs_and_default_cwd(tmp_path):
    plan = p("""
name: m01
max_parallel: 2
defaults: {profile: background, mem: 6G}
steps:
  - name: sort
    foreach: [a, b, c]
    cmd: python sort.py --session {item}
    retries: 1
  - name: summary
    needs: [sort]
    cmd: python summarise.py
""", tmp_path)
    keys = [s["key"] for s in plan.steps]
    assert keys == ["sort[a]", "sort[b]", "sort[c]", "summary"]
    assert plan.steps[1]["cmd"] == "python sort.py --session b"
    assert plan.steps[3]["needs"] == ["sort[a]", "sort[b]", "sort[c]"]
    assert plan.steps[0]["mem_gb"] == 6.0
    assert plan.steps[0]["retries"] == 1 and plan.steps[3]["retries"] == 0
    # spec lives in <repo>/jobs -> cwd defaults to <repo>
    assert plan.steps[0]["cwd"] == str(tmp_path.resolve())
    assert plan.max_parallel == 2


def test_sequential_mode_chains_steps(tmp_path):
    plan = p("""
name: seq
mode: sequential
steps:
  - {name: a, cmd: x}
  - {name: b, foreach: [1, 2], cmd: "y {item}"}
  - {name: c, cmd: z}
""", tmp_path)
    by = {s["key"]: s for s in plan.steps}
    assert by["a"]["needs"] == []
    assert by["b[1]"]["needs"] == ["a"] and by["b[2]"]["needs"] == ["a"]
    assert by["c"]["needs"] == ["b[1]", "b[2]"]


def test_pairwise_needs_with_item_substitution(tmp_path):
    plan = p("""
name: pw
steps:
  - {name: sort, foreach: [s1, s2], cmd: "sort {item}"}
  - {name: qc, foreach: [s1, s2], needs: ["sort[{item}]"], cmd: "qc {item}"}
""", tmp_path)
    by = {s["key"]: s for s in plan.steps}
    assert by["qc[s1]"]["needs"] == ["sort[s1]"]
    assert by["qc[s2]"]["needs"] == ["sort[s2]"]


def test_dict_items(tmp_path):
    plan = p("""
name: d
steps:
  - name: fit
    foreach: [{name: m1, lam: 0.1}, {name: m2, lam: 1}]
    cmd: "fit --lam {item.lam} --tag {item.name} --i {index}"
""", tmp_path)
    assert [s["key"] for s in plan.steps] == ["fit[m1]", "fit[m2]"]
    assert plan.steps[1]["cmd"] == "fit --lam 1 --tag m2 --i 1"


def test_collects_all_errors(tmp_path):
    with pytest.raises(SpecError) as e:
        p("""
name: bad
defaults: {profil: x}
steps:
  - {name: a, cmd: x, mem: lots, profile: nope, needs: [ghost], timeout: soon}
  - {name: a, cmd: y}
""", tmp_path)
    msg = " | ".join(e.value.errors)
    assert "profil" in msg and "duplicate" in msg


def test_field_errors(tmp_path):
    with pytest.raises(SpecError) as e:
        p("""
name: bad
steps:
  - {name: a, cmd: x, mem: lots, profile: nope, needs: [ghost], timeout: soon, on_fail: maybe}
""", tmp_path)
    msg = " | ".join(e.value.errors)
    for frag in ("bad size", "unknown profile", "unknown step 'ghost'", "bad duration", "on_fail"):
        assert frag in msg


def test_cycle_detected(tmp_path):
    with pytest.raises(SpecError) as e:
        p("""
name: cyc
steps:
  - {name: a, cmd: x, needs: [b]}
  - {name: b, cmd: y, needs: [a]}
""", tmp_path)
    assert "cycle" in str(e.value)


def test_missing_env_and_cwd(tmp_path):
    with pytest.raises(SpecError) as e:
        p("""
name: env
steps:
  - {name: a, cmd: x, env: nosuchenv, cwd: Z:/definitely/not/here}
""", tmp_path, env_exists=lambda n: n == "real")
    msg = str(e.value)
    assert "nosuchenv" in msg and "cwd does not exist" in msg


def test_start_at_tomorrow_if_past():
    from procdeck.spec import parse_start_at
    now = dt.datetime(2026, 11, 12, 23, 0)
    t = dt.datetime.fromtimestamp(parse_start_at("22:00", now))
    assert t == dt.datetime(2026, 11, 13, 22, 0)
    t = dt.datetime.fromtimestamp(parse_start_at("23:30", now))
    assert t == dt.datetime(2026, 11, 12, 23, 30)


def test_substitute_leaves_other_braces():
    assert substitute('python -c "d={1: 2}" {item}', "x", 0) == 'python -c "d={1: 2}" x'
