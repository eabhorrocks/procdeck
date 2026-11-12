from procdeck import labels
from procdeck.alerts import level_for, linear_slope, predict_full_s
from procdeck.config import format_cores, parse_cores, parse_duration_s, parse_size_gb
from procdeck.sampler import Row, assign_groups, is_orphan

CFG = {"ram_warn": 85, "ram_critical": 93, "commit_warn": 90, "eta_warn_minutes": 5}


def test_python_script_label():
    k, lab, key = labels.describe("python.exe", ["python", "-u", r"D:\Code\SpikePostProcessing\sort.py", "--session", "M01"],
                                  r"C:\Users\x")
    assert k == "script" and lab == "SpikePostProcessing › sort.py --session M01"
    assert key == "SpikePostProcessing/sort.py"


def test_relative_script_uses_cwd():
    _, lab, key = labels.describe("python.exe", ["python", "fit.py"], r"D:\Code\multi-area-analysis")
    assert lab == "multi-area-analysis › fit.py" and key == "multi-area-analysis/fit.py"


def test_conda_run_label():
    cmd = [r"C:\miniconda3\python.exe", r"C:\miniconda3\Scripts\conda-script.py", "run", "-n", "eegDynamics_env",
           "--no-capture-output", "python", "scan.py", "--workers", "6"]
    _, lab, key = labels.describe("python.exe", cmd, r"D:\Code\cross-species-oscillator")
    assert lab == "cross-species-oscillator › scan.py --workers 6 [eegDynamics_env]"
    assert key == "cross-species-oscillator/scan.py"


def test_worker_and_kernel_labels():
    assert labels.describe("python.exe", ["python", "-c", "from joblib.externals.loky.backend.popen_loky_win32 import main"],
                           None)[1] == "loky worker"
    assert labels.describe("python.exe", ["python", "-m", "ipykernel_launcher", "-f", "k.json"],
                           r"D:\Code\proj")[1] == "proj › Jupyter kernel"


def test_matlab_labels_and_thread_injection():
    assert labels.describe("MATLAB.exe", ["matlab.exe"], None)[1] == "MATLAB (desktop)"
    _, lab, key = labels.describe("MATLAB.exe", ["matlab", "-batch", "make_figs('M01')"], r"D:\Code\figs")
    assert lab.startswith("figs › MATLAB -batch make_figs") and key == "figs/matlab:make_figs"
    assert labels.inject_matlab_threads('matlab -batch "make_figs(1)"', 4) == 'matlab -batch "maxNumCompThreads(4); make_figs(1)"'


def test_split_first_token():
    assert labels.split_first_token('"C:\\Program Files\\MATLAB\\bin\\matlab.exe" -batch "x"') == (
        "C:\\Program Files\\MATLAB\\bin\\matlab.exe", '-batch "x"')
    assert labels.split_first_token("python a.py") == ("python", "a.py")


def test_script_key_from_cmd():
    assert labels.script_key_from_cmd("python sort.py --session {x}", r"D:\Code\Spike") == "Spike/sort.py"


def test_parse_helpers():
    assert parse_cores("0-2,5", 12) == [0, 1, 2, 5]
    assert parse_cores("all", 4) == [0, 1, 2, 3]
    assert format_cores([0, 1, 2, 5, 6], 12) == "0-2,5-6"
    assert format_cores(list(range(12)), 12) == "all"
    assert parse_size_gb("512M") == 0.5 and parse_size_gb("6G") == 6 and parse_size_gb(3) == 3
    assert parse_duration_s("1h30m") == 5400 and parse_duration_s("45s") == 45


def test_slope_and_eta():
    pts = [(t, 1000.0 + 50 * t) for t in range(10)]
    assert abs(linear_slope(pts) - 50) < 1e-9
    assert predict_full_s(5000, 50) is None  # tiny growth is noise
    assert predict_full_s(600e6, 1e6) == 600


def test_levels():
    assert level_for(50, 50, None, CFG) == "ok"
    assert level_for(86, 50, None, CFG) == "warn"
    assert level_for(94, 50, None, CFG) == "critical"
    assert level_for(70, 50, 120, CFG) == "warn"   # predicted full in 2 min
    assert level_for(70, 92, None, CFG) == "warn"  # commit nearly exhausted


def _row(pid, ppid, name, ctime, own=True):
    return Row(pid=pid, ppid=ppid, name=name, ctime=ctime, own=own)


def test_orphan_and_pid_reuse():
    rows = {1: _row(1, 0, "explorer.exe", 0), 10: _row(10, 99, "python.exe", 5),
            11: _row(11, 12, "python.exe", 5), 12: _row(12, 1, "cmd.exe", 9)}  # 12 is younger than 11 -> reused PID
    watch = ["python.exe"]
    assert is_orphan(rows, rows[10], watch)
    assert is_orphan(rows, rows[11], watch)
    assert not is_orphan(rows, rows[12], watch)


def test_grouping():
    rows = {
        1: _row(1, 0, "explorer.exe", 0),
        2: _row(2, 1, "pwsh.exe", 1),
        3: _row(3, 2, "python.exe", 2),      # job root
        4: _row(4, 3, "python.exe", 3),      # worker -> same job
        5: _row(5, 4, "conhost.exe", 4),     # spawned by job -> same job
        6: _row(6, 1, "chrome.exe", 1), 7: _row(7, 6, "chrome.exe", 2),
        8: _row(8, 0, "svchost.exe", 0, own=False),
        9: _row(9, 2, "pythonw.exe", 5),     # launcher -> managed
        20: _row(20, 9, "python.exe", 6),
    }
    g = assign_groups(rows, {9: {"run": "r", "step": "s"}}, ["python.exe", "pythonw.exe"])
    assert g[3] == g[4] == g[5] == ("job", 3)
    assert g[6] == g[7] == ("app", "chrome.exe")
    assert g[8] == ("system",)
    assert g[9] == g[20] == ("managed", 9)
    assert g[2] == ("app", "pwsh.exe")
