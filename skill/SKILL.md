---
name: procdeck
description: Run, queue and schedule long analyses (Python / MATLAB) on this Windows workstation with procdeck - write a pipeline spec (parallel, sequential, fan-out over sessions/mice), dry-run it, submit it, then check status and logs, retry, cancel, or steer CPU/RAM of running jobs. Use when the user asks to run, queue, batch or schedule analyses or jobs, run something overnight / after another job, limit how much CPU or RAM a job gets, or asks what is using the CPU or RAM right now.
---

# procdeck: running analyses on this machine

procdeck is a local daemon (dashboard at http://127.0.0.1:8857) that runs pipelines of analysis steps within the
machine's budget: 6 cores / 12 logical CPUs and 16 GB of RAM. Usually RAM runs out first. It also monitors and steers
processes it did not start.

**CLI** (the daemon starts automatically if it is not running):
- Git Bash: `/d/Code/procdeck/bin/procdeck <command>`
- PowerShell / cmd: `D:\Code\procdeck\bin\procdeck.cmd <command>`

Every read command has `--json`.

## Workflow for "run X for these sessions" requests

1. **Look first.** Read the analysis script(s) to find the command-line arguments, which conda env the project uses, and
   whether the script has its own worker/`n_jobs` option. Run `procdeck history <script>` to see past runtimes and peak
   RAM, and `procdeck top` to see current load.
2. **Write the spec** into the analysis repo at `jobs/<name>.yaml`. Steps run from the repo root by default; see the
   spec reference below.
3. **Dry-run:** `procdeck submit jobs/<name>.yaml --dry-run`. This validates the spec (and lists every error),
   resolves the conda env's python, and shows the order, thread/RAM budget and an estimated finish time. Fix any
   errors and run it again.
4. **Show the user the plan** (steps, parallelism, ETA) and get a yes before submitting. Then submit with
   `procdeck submit jobs/<name>.yaml --by claude`.
5. **Report the run id.** The daemon owns the run from then on, and it survives this Claude session ending. Later:
   `procdeck status <name-or-id>`, `procdeck logs <run> <step> -n 80`, `procdeck retry <run> [step]`,
   `procdeck cancel <run> [step]`.

## Spec reference

```yaml
name: m01-october                  # required; letters digits _ . -
description: Spike sorting + summary for M01's October sessions
priority: 0                        # higher-priority runs get free slots first
mode: parallel                     # or 'sequential': each step needs the previous one (unless it lists needs)
max_parallel: 2                    # cap on concurrently running steps of this run
defaults:                          # apply to every step unless the step overrides them
  cwd: D:/Code/SpikePostProcessing # default: repo root (parent of jobs/), else the spec's folder
  env: spikepost                   # conda env name -> that env's python + DLL paths; omit for MATLAB
  profile: background              # resource profile (see below)
  mem: 6G                          # expected PEAK RAM per step instance; drives admission control
  retries: 0
  timeout: 3h                      # kill the step after this long
  on_fail: stop                    # 'continue' lets dependants run even if this step fails
  env_vars: {MY_FLAG: "1"}
  threads: 6                       # override the profile's thread count
steps:
  - name: sort
    foreach: [M01_2026-10-01, M01_2026-10-02, M01_2026-10-07]   # one instance per item: sort[M01_2026-10-01], ...
    cmd: python sort.py --session {item} --n-jobs %PROCDECK_WORKERS%
    shell: true                    # needed only for %VARS%, pipes or redirects (runs via cmd.exe)
    max_parallel: 1                # per-step cap (e.g. a step that saturates disk I/O)
  - name: qc
    foreach: [M01_2026-10-01, M01_2026-10-02, M01_2026-10-07]
    needs: ["sort[{item}]"]        # pairwise: qc[X] waits only for sort[X]
    cmd: python qc.py {item}
  - name: summary
    needs: [sort]                  # a step name = all of its instances
    cmd: python summarise.py --mouse M01
  - name: figs
    needs: [summary]
    cmd: matlab -batch "make_figs('M01')"   # procdeck prepends maxNumCompThreads(N);
    profile: heavy
  - name: overnight_fit
    start_at: "22:00"              # or "2026-11-13 08:00"; HH:MM means the next occurrence
    cmd: python fit.py
  - name: when_free
    when: idle                     # starts once machine CPU has been <20% for 5 min
    cmd: python big_reindex.py
```

- Substitutions in `cmd`, `cwd`, `env_vars` and `needs`: `{item}` and `{index}`. For dict items, use `{item.field}`.
  Dict items are named by their `name` / `id` / `session` field.
- Other braces are left alone, so `python -c "d={1: 2}"` is fine.
- Prefer passing parallelism from the environment: scripts can read `os.environ["PROCDECK_WORKERS"]` for
  `n_jobs` / pool size. In MATLAB, use `parpool(str2double(getenv('PROCDECK_WORKERS')))`.
- `OMP/MKL/OPENBLAS/NUMEXPR/NUMBA_NUM_THREADS` and `LOKY_MAX_CPU_COUNT` are set to the step's thread count
  automatically.
- Use `matlab -batch "..."`, not `-r`. `-batch` waits for completion and returns a proper exit code.
- A step succeeds when its exit code is 0. Output goes to the step log (`procdeck logs`). Python output is unbuffered.

### Profiles (`config/profiles.yaml`)

| profile | cores | threads | priority | CPU share |
|---|---|---|---|---|
| default | all | 12 | normal | - |
| heavy | all | 10 | normal | weight 8 |
| background | 6-11 (physical cores 3-5) | 6 | below_normal | weight 2 |
| tiny | 10-11 | 2 | idle | - |

### Admission control (why a step is "held")

A ready step starts only when all of these hold:
- the run's and the step's `max_parallel` allow it;
- the threads of running steps plus this step fit the budget (12). One step may always run.
- available RAM, minus 1.5 GB headroom, minus the remaining expected growth of running steps, is at least the step's
  `mem`.

When there is no `mem`, the estimate is the step's past peak × 1.1 from history, else 1 GB. **Always set `mem` for
heavy steps.** Estimate it from history, the data size, or a quick test run. Too low risks swapping; too high
serialises everything.

At 93% RAM, admission pauses automatically until memory recovers. The held reason appears in `procdeck status` and in
the dashboard.

## Other commands

| command | what it does |
|---|---|
| `procdeck top [-n 20]` | live CPU/RAM per job/app, with flags (not responding, parent exited, idle hog) |
| `procdeck steer PID --weight 2` / `--cap 30` / `--cores 6-11` / `--priority below_normal` / `--profile background` / `--mem-cap 8` | steer a running process tree, including one procdeck did not start |
| `procdeck kill PID` / `suspend PID` / `resume-proc PID` | act on a whole process tree |
| `procdeck pause RUN` / `resume RUN` / `prio RUN N` | control a run |
| `procdeck admission pause` / `resume` | stop or allow starting any queued steps |
| `procdeck open` | open the dashboard |

Guidelines:
- Steering or killing processes the user did not ask about needs their OK.
- Never set `--mem-cap` without saying that the job will crash with MemoryError when it reaches the cap.
- `procdeck status` and `procdeck top` are safe to run at any time.
