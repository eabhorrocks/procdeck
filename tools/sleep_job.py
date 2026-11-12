"""Sleep for N seconds, print progress, exit with a chosen code. Also prints thread-related env vars.

python tools/sleep_job.py --seconds 5 --code 0
"""
import argparse
import os
import sys
import time

ap = argparse.ArgumentParser()
ap.add_argument("--seconds", type=float, default=5)
ap.add_argument("--code", type=int, default=0)
ap.add_argument("--label", default="")
a = ap.parse_args()
print(f"sleep_job {a.label} pid={os.getpid()} python={sys.executable}", flush=True)
for v in ("PROCDECK_RUN", "PROCDECK_STEP", "PROCDECK_WORKERS", "OMP_NUM_THREADS", "CONDA_DEFAULT_ENV"):
    print(f"  {v}={os.environ.get(v)}", flush=True)
for i in range(int(a.seconds)):
    time.sleep(1)
    print(f"tick {i + 1}/{int(a.seconds)}", flush=True)
sys.exit(a.code)
