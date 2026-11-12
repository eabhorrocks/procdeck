"""Spawn N child processes that each sleep; the parent sleeps too. For testing kill-tree and job inheritance.

python tools/spawn_tree.py --children 3 --seconds 60
"""
import argparse
import subprocess
import sys
import time

ap = argparse.ArgumentParser()
ap.add_argument("--children", type=int, default=3)
ap.add_argument("--seconds", type=float, default=60)
a = ap.parse_args()
kids = [subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({a.seconds})"]) for _ in range(a.children)]
print("children:", [k.pid for k in kids], flush=True)
time.sleep(a.seconds)
