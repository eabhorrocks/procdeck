"""Allocate RAM gradually, hold it, then exit. For testing admission control and memory alerts.

python tools/eat_ram.py --gb 4 --rate 1 --hold 20 [--fail]
"""
import argparse
import sys
import time

ap = argparse.ArgumentParser()
ap.add_argument("--gb", type=float, default=2.0, help="total GB to allocate")
ap.add_argument("--rate", type=float, default=1.0, help="GB per second")
ap.add_argument("--hold", type=float, default=10.0, help="seconds to hold after allocating")
ap.add_argument("--fail", action="store_true", help="exit with code 3 at the end")
ap.add_argument("--fail-once-file", help="fail if this file does not exist yet (creates it), succeed otherwise")
a = ap.parse_args()

chunk = 64 * 1024 * 1024
blocks = []
target = int(a.gb * 1024 ** 3)
t0 = time.time()
while len(blocks) * chunk < target:
    blocks.append(bytearray(chunk))  # bytearray is zero-filled -> pages are committed and touched
    done = len(blocks) * chunk
    wait = done / (a.rate * 1024 ** 3) - (time.time() - t0)
    if wait > 0:
        time.sleep(wait)
    if len(blocks) % 8 == 0:
        print(f"allocated {done / 1024 ** 3:.2f} GB", flush=True)
print(f"holding {len(blocks) * chunk / 1024 ** 3:.2f} GB for {a.hold}s", flush=True)
time.sleep(a.hold)
if a.fail_once_file:
    import os
    if not os.path.exists(a.fail_once_file):
        open(a.fail_once_file, "w").close()
        print("failing on purpose (first attempt)", flush=True)
        sys.exit(4)
if a.fail:
    print("failing on purpose", flush=True)
    sys.exit(3)
print("done", flush=True)
