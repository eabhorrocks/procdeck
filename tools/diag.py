"""Diagnose why the CLI cannot reach the daemon from a particular shell.

python tools/diag.py
"""
import os
import socket
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from procdeck.config import TOKEN_PATH, get_token, load_settings  # noqa: E402

port = load_settings()["port"]
print("python:", sys.executable)
print("cwd:", os.getcwd())
print("proxy env vars:", {k: v for k, v in os.environ.items() if "proxy" in k.lower()} or "none")
print("urllib proxies:", urllib.request.getproxies() or "none")
print("token file:", TOKEN_PATH, "exists" if TOKEN_PATH.exists() else "MISSING")
s = socket.socket()
s.settimeout(3)
try:
    s.connect(("127.0.0.1", port))
    print(f"tcp 127.0.0.1:{port}: connect OK")
except OSError as e:
    print(f"tcp 127.0.0.1:{port}: {e!r}")
finally:
    s.close()
for label, opener in (("default urllib", urllib.request.build_opener()),
                      ("no-proxy urllib", urllib.request.build_opener(urllib.request.ProxyHandler({})))):
    t = time.perf_counter()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/top?n=1", headers={"x-procdeck-token": get_token()})
        with opener.open(req, timeout=10) as r:
            print(f"{label}: HTTP {r.status} in {time.perf_counter() - t:.2f}s")
    except urllib.error.HTTPError as e:
        print(f"{label}: HTTP {e.code} after {time.perf_counter() - t:.2f}s: body={e.read()[:200]!r} "
              f"server={e.headers.get('server')!r}")
    except Exception as e:
        print(f"{label}: FAILED after {time.perf_counter() - t:.2f}s: {e!r}")
import hashlib  # noqa: E402
print("token fingerprint:", hashlib.sha256(get_token().encode()).hexdigest()[:8], "len", len(get_token()))
