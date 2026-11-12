"""Burn CPU on N worker processes for S seconds, printing work done per second (to compare CPU shares).

python tools/burn_cpu.py --workers 12 --seconds 30
"""
import argparse
import multiprocessing as mp
import time


def burn(seconds: float, q) -> None:
    end = time.time() + seconds
    n = 0
    while time.time() < end:
        for _ in range(20000):
            n += 1
    q.put(n)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seconds", type=float, default=20)
    a = ap.parse_args()
    q = mp.Queue()
    ps = [mp.Process(target=burn, args=(a.seconds, q)) for _ in range(a.workers)]
    t0 = time.time()
    for p in ps:
        p.start()
    total = sum(q.get() for _ in ps)
    for p in ps:
        p.join()
    print(f"{a.workers} workers, {time.time() - t0:.1f}s, work units {total / 1e6:.1f}M "
          f"({total / 1e6 / a.seconds:.2f}M/s)", flush=True)
