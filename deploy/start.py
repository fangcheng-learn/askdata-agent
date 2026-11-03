"""Run both services in one container; only Next.js listens publicly."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "references/askdata_studio/backend"


def main() -> int:
    port = int(os.environ.get("PORT", "10000"))
    if not 1 <= port <= 65535 or port == 8001:
        raise ValueError("PORT must be valid and different from the internal API port")
    children: list[subprocess.Popen] = []
    stopping = False

    def stop(_signal, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        children.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", "8001", "--workers", "1"], cwd=BACKEND))
        deadline = time.monotonic() + 90
        while not stopping:
            if children[0].poll() is not None:
                raise RuntimeError("Backend exited before readiness")
            try:
                with urlopen("http://127.0.0.1:8001/api/health", timeout=2) as response:
                    if response.status == 200:
                        break
            except (URLError, TimeoutError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Backend did not become ready within 90 seconds")
            time.sleep(0.2)
        if stopping:
            return 0
        env = {**os.environ, "PORT": str(port), "HOSTNAME": "0.0.0.0",
               "API_PROXY_TARGET": "http://127.0.0.1:8001"}
        children.append(subprocess.Popen(
            ["node", str(ROOT / "apps/insightpilot/server.js")], cwd=ROOT, env=env))
        while not stopping:
            if any(child.poll() is not None for child in children):
                raise RuntimeError("A service exited; stopping the container")
            time.sleep(0.25)
        return 0
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        deadline = time.monotonic() + 10
        for child in children:
            try:
                child.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
