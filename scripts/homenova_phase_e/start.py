#!/usr/bin/env python3
"""Start InsightPilot with the reused DataFoundry frontend and existing analysis service."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "references/askdata_studio"
FRONTEND = ROOT / "apps/insightpilot"
PRIVATE = ROOT / "data/homenova_phase_e/private"
NODE = Path("/Users/fangchengdemac/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node")


def available(url):
    try:
        with urlopen(url, timeout=2) as response:
            return response.status == 200
    except (URLError, OSError):
        return False


def listener(port):
    result = subprocess.run(["lsof", "-nP", "-iTCP:" + str(port), "-sTCP:LISTEN", "-Fp"],
                            capture_output=True, text=True)
    return [int(line[1:]) for line in result.stdout.splitlines() if line.startswith("p")]


def launch(command, cwd, logfile, env=None):
    with logfile.open("ab") as stream:
        process = subprocess.Popen(command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, start_new_session=True, env=env)
    return process.pid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "restart", "status"))
    parser.add_argument("--model", help="Optional model name; existing running model takes precedence by default")
    parser.add_argument("--record-dir", type=Path, help="Write new runtime records outside historical E evidence")
    parser.add_argument("--restart-frontend", action="store_true", help="With restart, also restart only this project's frontend")
    parser.add_argument("--frontend-only", action="store_true", help="With restart, replace only the frontend and retain backend jobs and login sessions")
    args = parser.parse_args()
    if args.restart_frontend and args.action != 'restart':
        parser.error('--restart-frontend requires restart')
    if args.frontend_only and args.action != 'restart':
        parser.error('--frontend-only requires restart')
    global PRIVATE
    if args.record_dir:
        PRIVATE = args.record_dir.resolve()
    PRIVATE.mkdir(parents=True, exist_ok=True)
    previous_status = {}
    if available("http://127.0.0.1:8001/api/health"):
        with urlopen("http://127.0.0.1:8001/api/health", timeout=3) as response:
            previous_status = json.load(response)
    child_env = os.environ.copy()
    # User requested pausing independent report content review; retain an explicit switch.
    child_env.setdefault("REPORT_CONTENT_REVIEW_ENABLED", "false")
    if args.frontend_only and not previous_status:
        raise RuntimeError("Backend is unavailable; frontend-only restart will not start or change it")
    model = args.model or previous_status.get("llm_model") or child_env.get("LLM_MODEL")
    if args.action != "status" and not args.frontend_only:
        sys.path.insert(0, str(APP / "backend"))
        from app.config import load_env
        if os.environ.get("LLM_API_KEY") and not child_env.get("LLM_API_KEY"):
            child_env["LLM_API_KEY"] = os.environ["LLM_API_KEY"]
        model = model or os.environ.get("LLM_MODEL")
        shared = {}
        if not child_env.get("LLM_API_KEY"):
            shared = load_env(Path("/Users/fangchengdemac/Documents/软件开发知识/.env"),
                              allowed_keys={"DASHSCOPE_API_KEY", "QWEN_MODEL"})
            key = shared.get("DASHSCOPE_API_KEY")
            if not key:
                raise RuntimeError("DASHSCOPE_API_KEY is missing; no credential value was logged")
            child_env["LLM_API_KEY"] = key
        model = model or shared.get("QWEN_MODEL") or "qwen3.7-plus"
        child_env["LLM_MODEL"] = model
    if args.action == "restart" and not args.frontend_only:
        for pid in listener(8001):
            command = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True)
            expected = str(APP / "backend/.venv/bin/python")
            if expected not in command or "-m uvicorn app.main:app" not in command:
                raise RuntimeError("Port 8001 belongs to another process; refusing to stop it")
            os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if not listener(8001):
                break
            time.sleep(0.1)
        if listener(8001):
            raise RuntimeError("Previous V2 backend did not stop")
    if args.action == "restart":
        if args.restart_frontend or args.frontend_only:
            for pid in listener(5174):
                command = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True)
                expected = str(APP / "frontend/node_modules/vite/bin/vite.js")
                cwd_info = subprocess.check_output(['lsof','-a','-p',str(pid),'-d','cwd','-Fn'], text=True)
                old_vite = expected in command and '--host 127.0.0.1' in command
                native_next = ('next-server' in command or '/next/dist/bin/next' in command) and ('n'+str(FRONTEND)+'\n') in cwd_info
                if not (old_vite or native_next):
                    raise RuntimeError('Port 5174 belongs to another process; refusing to stop it')
                os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                if not listener(5174):
                    break
                time.sleep(0.1)
            if listener(5174):
                raise RuntimeError('Previous V2 frontend did not stop')
    started = {}
    if args.action != "status":
        if not available("http://127.0.0.1:8001/api/health"):
            if args.frontend_only:
                raise RuntimeError("Backend became unavailable; frontend-only restart will not start or change it")
            if listener(8001):
                raise RuntimeError("Port 8001 is occupied but its health endpoint is unavailable")
            started["backend"] = launch(
                [str(APP / "backend/.venv/bin/python"), "-m", "uvicorn", "app.main:app",
                 "--host", "127.0.0.1", "--port", "8001"],
                APP / "backend", PRIVATE / "backend.runtime.log", env=child_env)
        if not available("http://127.0.0.1:5174"):
            if listener(5174):
                raise RuntimeError("Port 5174 is occupied by an unavailable frontend")
            started["frontend"] = launch(
                [str(NODE), str(ROOT / 'node_modules/next/dist/bin/next'), 'start', '--hostname', '127.0.0.1', '--port', '5174'],
                FRONTEND, PRIVATE / 'frontend.runtime.log')
        for _ in range(100):
            if available("http://127.0.0.1:8001/api/health") and available("http://127.0.0.1:5174"):
                break
            time.sleep(0.2)
    result = {
        "project": str(APP), "frontend_project": str(FRONTEND), "backend_url": "http://127.0.0.1:8001",
        "frontend_url": "http://127.0.0.1:5174",
        "backend_healthy": available("http://127.0.0.1:8001/api/health"),
        "frontend_healthy": available("http://127.0.0.1:5174"),
        "backend_pids": listener(8001), "frontend_pids": listener(5174), "started": started,
        "authentication": "Existing login endpoint and account policies; tokens expire on backend restart.",
        "model": model,
    }
    (PRIVATE / "startup.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(result, ensure_ascii=False))
    if not result["backend_healthy"] or not result["frontend_healthy"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
