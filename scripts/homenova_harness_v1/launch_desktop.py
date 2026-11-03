#!/usr/bin/env python3
"""Desktop entry point; reuse the project launcher without restarting services."""
from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / 'references/askdata_studio'
FRONTEND = ROOT / 'apps/insightpilot'
PYTHON = APP / 'backend/.venv/bin/python'
START = ROOT / 'scripts/homenova_phase_e/start.py'
RUNTIME = ROOT / 'data/homenova_harness_v1/private/desktop_launcher'
URL = 'http://127.0.0.1:5174/'


def check_port_owners() -> None:
    expected = {
        8001: (str(PYTHON), '-m uvicorn app.main:app'),
        5174: (str(APP / 'frontend/node_modules/vite/bin/vite.js'),),
    }
    for port, signatures in expected.items():
        result = subprocess.run(['/usr/sbin/lsof', '-nP', '-iTCP:' + str(port),
                                 '-sTCP:LISTEN', '-Fp'], capture_output=True, text=True)
        if result.returncode not in {0, 1}:
            raise RuntimeError(f'无法检查端口 {port}，未尝试启动其他服务。')
        pids = {line[1:] for line in result.stdout.splitlines() if line.startswith('p')}
        for pid in pids:
            process = subprocess.run(['/bin/ps', '-p', pid, '-o', 'command='],
                                     capture_output=True, text=True, check=True)
            owned = all(signature in process.stdout for signature in signatures)
            if port == 5174 and not owned:
                cwd_info = subprocess.run(['/usr/sbin/lsof', '-a', '-p', pid, '-d', 'cwd', '-Fn'],
                                          capture_output=True, text=True, check=True)
                owned = ('next-server' in process.stdout or '/next/dist/bin/next' in process.stdout) and (
                    'n' + str(FRONTEND) in cwd_info.stdout.splitlines())
            if not owned:
                raise RuntimeError(f'端口 {port} 已被其他程序占用。为保护其他项目，本次未启动。')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-open', action='store_true', help='Check/start services without opening a browser')
    args = parser.parse_args()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with (RUNTIME / 'launcher.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0  # The first double-click is already starting the same app.
        try:
            if not PYTHON.exists() or not START.is_file():
                raise RuntimeError('项目或 Python 环境不存在，请检查 dataagent 项目位置。')
            check_port_owners()
            env = os.environ.copy()
            env['PATH'] = '/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin'
            env.setdefault('MODEL_TRACE_DIR', str(RUNTIME / 'runtime/model_traces'))
            with (RUNTIME / 'launcher.log').open('a', encoding='utf-8') as stream:
                stream.write('\n[' + datetime.now().astimezone().isoformat() + '] launch\n')
                stream.flush()
                subprocess.run([str(PYTHON), str(START), 'start', '--record-dir', str(RUNTIME / 'runtime')],
                               cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT,
                               timeout=90, check=True)
            check_port_owners()
            status = json.loads((RUNTIME / 'runtime/startup.json').read_text())
            if not status.get('backend_healthy') or not status.get('frontend_healthy'):
                raise RuntimeError('服务未通过启动检查，请查看启动日志。')
            if not args.no_open:
                subprocess.run(['/usr/bin/open', '-a', 'Safari', URL], check=True)
            print(json.dumps({'ready': True, 'url': URL, 'browser_opened': not args.no_open,
                              'started': status.get('started', {})}, ensure_ascii=False))
            return 0
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            message = ('AskData Studio 启动未完成\n\n' + str(error)
                       + '\n\n日志：' + str(RUNTIME / 'launcher.log') + '\n')
            failure = RUNTIME / '启动错误.txt'
            failure.write_text(message, encoding='utf-8')
            print(message, file=sys.stderr)
            if not args.no_open:
                subprocess.run(['/usr/bin/open', str(failure)], check=False)
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
