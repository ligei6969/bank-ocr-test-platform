"""Restart only the two verified local OCR servers, preserving their environments."""
import json
import subprocess
import time
from pathlib import Path

import httpx
import psutil

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent


def restart(pid, port, reload=False):
    process = psutil.Process(pid)
    assert Path(process.cwd()).resolve() == ROOT
    args = ' '.join(process.cmdline())
    assert 'uvicorn' in args or 'spawn_main' in args
    executable, environment = process.exe(), process.environ()
    process.terminate()
    process.wait(timeout=10)
    command = [executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
               '--port', str(port), '--log-config',
               str(ROOT / 'reports/test-artifacts/geometry-review/logging.json')]
    if reload:
        command += ['--reload', '--reload-dir', str(ROOT / 'app')]
    with (OUT / f'backend-{port}.log').open('ab') as log:
        child = subprocess.Popen(command, cwd=ROOT, env=environment, stdout=log,
                                 stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
    with httpx.Client(trust_env=False, timeout=2) as client:
        for _ in range(60):
            if child.poll() is not None:
                raise RuntimeError(f'Server {port} exited: {child.returncode}')
            try:
                if client.get(f'http://127.0.0.1:{port}/login').status_code == 200:
                    return {'port': port, 'pid': child.pid, 'ocr_mode': environment.get('OCR_MODE'),
                            'ready': True, 'reload': reload}
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
    raise RuntimeError(f'Server {port} did not become ready')


if __name__ == '__main__':
    results = [restart(44992, 8002), restart(31128, 8001, reload=True)]
    (OUT / 'runtime.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(json.dumps(results))
