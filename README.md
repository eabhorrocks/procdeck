# procdeck

A local job runner, process monitor and resource steerer for a Windows analysis workstation.

It replaces Task Manager for three jobs:

- **See what's running.** Each analysis is listed by its script and folder (e.g.
  `cross-species-oscillator › scan.py --workers 6 [eegDynamics_env]`), not as one of a dozen `python.exe` rows. Worker
  processes are grouped under their job. Hung, orphaned, idle-but-RAM-heavy and suspended processes are flagged.
- **Run analyses as pipelines.** Steps can run in parallel, in sequence, or fanned out over sessions. They start only
  when there is RAM and CPU for them; failures retry; everything is logged. Claude writes the pipeline specs (see
  `skill/`).
- **Steer running jobs.** Change priority, pin cores, set a CPU weight or hard cap, or set a memory cap on any process
  tree. RAM alerts give a time-to-full prediction and name the process that's growing.

Dashboard: `procdeck open` → http://127.0.0.1:8857

## Install

```bash
conda env create -f environment.yml        # creates env "procdeck" and installs this repo in editable mode
```

```bash
D:/Code/procdeck/bin/procdeck start        # Git Bash  (PowerShell: D:\Code\procdeck\bin\procdeck.cmd start)
```

```bash
D:/Code/procdeck/bin/procdeck install-skill   # copy the Claude skill to ~/.claude/skills/procdeck
```

```bash
D:/Code/procdeck/bin/procdeck autostart on    # optional: start the daemon at login
```

## Use

```bash
procdeck submit jobs/m01.yaml --dry-run    # validate, resolve envs, show order / RAM / ETA
procdeck submit jobs/m01.yaml --by claude
procdeck status [run]                      # runs, or one run's steps (with why a step is held)
procdeck logs <run> <step> -f
procdeck retry|cancel <run> [step]   ·   procdeck pause|resume <run>   ·   procdeck prio <run> 5
procdeck top                               # live CPU/RAM per job
procdeck steer <pid> --weight 2 | --cap 30 | --cores 6-11 | --priority below_normal | --profile background
procdeck kill|suspend|resume-proc <pid>    # whole tree
procdeck history [script]                  # typical time / peak RAM per script
```

For the spec format, see [skill/SKILL.md](skill/SKILL.md). Examples are in [examples/](examples/) and
[skill/examples/](skill/examples/).

Config lives in [config/](config/): `settings.yaml` (alerts, admission, flags), `profiles.yaml` (cores, threads,
priority, CPU share) and `rules.yaml` (auto-apply settings to matching processes). Edit the files directly or in the
dashboard. The daemon reloads within 5 s.

## How it works

| piece | file | notes |
|---|---|---|
| sampler | `procdeck/ntsnap.py`, `sampler.py` | one `NtQuerySystemInformation` call per tick for every process (~20 ms). psutil's per-process calls were O(n²) and took 8–19 s per tick under load |
| labels | `labels.py` | script/module/`conda run`/MATLAB `-batch` → readable label and history key |
| alerts | `alerts.py` | RAM/commit thresholds, a 60 s linear fit for time-to-full, and the fastest-growing job; Windows toast on escalation |
| runner | `spec.py`, `runner.py`, `launch.py`, `condaenv.py` | spec → step instances; admission by `max_parallel`, thread budget and RAM (minus expected growth of running steps); conda activation cached per env |
| launcher | `launcher.py` | one per step; puts itself in a named **job object** (cores, CPU weight/cap, memory cap), so every child inherits the limits. It survives daemon restarts, and the daemon re-attaches by PID + create time |
| steering | `control.py`, `win32.py` | priority and affinity per process; CPU share and memory cap through a job object |
| store | `store.py` | SQLite in `%LOCALAPPDATA%\procdeck` (runs, steps, 5 s history, per-script stats) |
| API + UI | `server.py`, `web/` | FastAPI on 127.0.0.1 only, token header plus Origin check; plain JS dashboard |

### Behaviour worth knowing

- **CPU weight is relative.** It only matters while jobs compete for the CPU. Measured on this machine: two burners
  sharing one core with weights 8 and 2 got about a 3:1 split. A **hard cap** limits a job even when the CPU is idle.
- **Avoid `priority: idle`** while other jobs saturate the CPU. Idle-priority steps can starve completely; one sat for a
  minute without finishing Python start-up.
- **Memory caps make jobs crash** (MemoryError) when they reach the cap. Use them as a safety net only. Admission control
  plus a sensible `mem:` per step is the normal tool.
- **Pinning cores doesn't change the worker count.** A job started with 12 workers keeps 12, now sharing the pinned
  cores. Use `PROCDECK_WORKERS` / thread env vars at launch for real worker control.
- **No admin needed.** procdeck can only steer your own processes; system processes are shown read-only.

## Tests

```bash
"$LOCALAPPDATA/miniconda3/envs/procdeck/python.exe" -m pytest -q
```

[tools/](tools/) has load generators for manual end-to-end checks: `eat_ram.py`, `burn_cpu.py`, `spawn_tree.py`,
`sleep_job.py`.
