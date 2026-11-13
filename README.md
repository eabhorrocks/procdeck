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

Dashboard: `procdeck open` opens http://127.0.0.1:8857 in its own window (Edge app mode; pin it to the taskbar from
there). `procdeck open --browser` opens a normal tab instead; `dashboard.open_as` in `config/settings.yaml` sets the
default.

## Install

Windows only. Needs Miniconda or Anaconda.

```bash
git clone <repo-url> procdeck
```

```bash
cd procdeck
```

```bash
conda env create -f environment.yml        # creates env "procdeck" and installs this repo in editable mode
```

From the repo folder (PowerShell / cmd: `bin\procdeck.cmd` instead of `bin/procdeck`):

```bash
bin/procdeck start
```

```bash
bin/procdeck install-skill   # copy the Claude skill to ~/.claude/skills/procdeck, with this repo's paths filled in
```

```bash
bin/procdeck autostart on    # optional: start the daemon at login
```

The `bin` shims find the `procdeck` env in the usual conda locations; set `PROCDECK_PYTHON` to the env's
`python.exe` if yours is elsewhere. Add `bin` to your `PATH` to type plain `procdeck` (the rest of this README does),
or run `procdeck` directly inside `conda activate procdeck`.

## Use

```bash
procdeck submit jobs/m01.yaml --dry-run    # validate, resolve envs, show order / RAM / ETA
procdeck submit jobs/m01.yaml --by claude
procdeck status [run]                      # runs, or one run's steps (with why a step is held)
procdeck logs <run> <step> -f
procdeck retry|cancel <run> [step]   ·   procdeck pause|resume <run>   ·   procdeck prio <run> 5
procdeck move <run> <step> --before <other> | --first | --last   # reorder waiting steps (dashboard: drag the row)
procdeck parallel <run> <step>... | --max N   # run waiting steps together / raise the run's max_parallel
procdeck after <run> <step> [needs...]     # set what a waiting step waits for
procdeck forget <run>... | clear [--state cancelled] [-n]   # hide finished runs; logs + timing history kept
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
| store | `store.py` | SQLite in `~\.procdeck` (runs, steps, 5 s history, per-script stats), with logs and the access token |
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
- **Shells inside packaged apps** (such as terminals and agents in the Claude desktop app) have their `AppData`
  writes silently redirected into the app's private folder, and so does everything they start. procdeck therefore
  keeps its data in `~\.procdeck`, and `procdeck start` detects the redirection and launches the daemon through
  WMI, outside the app. Otherwise your jobs' MATLAB preferences, caches etc. would land in the app's private copy
  of `AppData`. Run `procdeck autostart` from a normal PowerShell window for the same reason.
- **Shells:** in PowerShell or cmd run `bin\procdeck.cmd`; in Git Bash run `bin/procdeck` (or put `bin` on
  your `PATH`).

## Tests

```bash
conda run -n procdeck python -m pytest -q
```

[tools/](tools/) has load generators for manual end-to-end checks: `eat_ram.py`, `burn_cpu.py`, `spawn_tree.py`,
`sleep_job.py`.

## License

MIT; see `LICENSE`.
