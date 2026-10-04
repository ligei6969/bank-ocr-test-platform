"""Authenticated loopback benchmark with isolated SQLite data and bounded load."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime
import hashlib
from importlib.metadata import distributions
import json
import os
from pathlib import Path
import platform
import secrets
import socket
import sqlite3
import subprocess
import sys
import sysconfig
import time

import httpx
import psutil

ROOT = Path(__file__).resolve().parents[1]


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 2)


async def stage(base: str, cookies: httpx.Cookies, token: str, image: bytes,
                concurrency: int, seconds: int, server: psutil.Process,
                expected: dict, request_ids: set[str]) -> dict:
    samples, errors, outcomes = [], Counter(), Counter()
    start_gate = asyncio.Event()
    start = deadline = 0.0
    stop_monitor = asyncio.Event()
    resources = []

    async def monitor():
        try:
            server.cpu_percent()
        except psutil.Error:
            return
        while not stop_monitor.is_set():
            try:
                resources.append({"cpu": server.cpu_percent(), "rss": server.memory_info().rss})
            except psutil.Error:
                break
            await asyncio.sleep(0.5)

    async def user():
        async with httpx.AsyncClient(base_url=base, cookies=cookies, trust_env=False,
                                     timeout=60, limits=httpx.Limits(max_connections=1)) as client:
            await start_gate.wait()
            while time.perf_counter() < deadline:
                began = time.perf_counter()
                error = None
                try:
                    response = await client.post("/bank-card/review", headers={"X-CSRF-Token": token},
                                                  files={"file": ("synthetic-load.png", image, "image/png")})
                    if response.status_code != 200:
                        error = f"HTTP_{response.status_code}"
                    else:
                        body = response.json()
                        outcomes[body.get("review_result", "missing")] += 1
                        comparable = {key: body.get(key) for key in expected}
                        if comparable != expected:
                            error = "inconsistent_business_response"
                        elif not isinstance(body.get("request_id"), str):
                            error = "missing_request_id"
                        elif body["request_id"] in request_ids:
                            error = "duplicate_request_id"
                        else:
                            request_ids.add(body["request_id"])
                except Exception as exc:
                    error = type(exc).__name__
                samples.append({"ms": (time.perf_counter() - began) * 1000, "success": error is None})
                if error:
                    errors[error] += 1

    users = [asyncio.create_task(user()) for _ in range(concurrency)]
    monitoring = asyncio.create_task(monitor())
    start = time.perf_counter()
    deadline = start + seconds
    start_gate.set()
    await asyncio.gather(*users)
    elapsed = time.perf_counter() - start
    stop_monitor.set()
    await monitoring
    latencies = [sample["ms"] for sample in samples]
    successes = sum(sample["success"] for sample in samples)
    return {"concurrency": concurrency, "dispatch_window_s": seconds, "elapsed_including_drain_s": round(elapsed, 3),
            "requests": len(samples), "successes": successes, "failures": len(samples) - successes,
            "failure_rate": (len(samples) - successes) / len(samples) if samples else None,
            "successful_rps": round(successes / elapsed, 2), "completed_rps": round(len(samples) / elapsed, 2),
            "latency_ms": {"p50": percentile(latencies, 50), "p95": percentile(latencies, 95),
                           "p99": percentile(latencies, 99), "max": round(max(latencies), 2) if latencies else None},
            "errors": dict(errors), "review_results": dict(outcomes),
            "server_cpu_percent_peak": max((r["cpu"] for r in resources), default=None),
            "server_rss_peak_mb": round(max((r["rss"] for r in resources), default=0) / 1024**2, 1)}


async def benchmark(args, output: Path, env: dict, image: bytes) -> dict:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    log_path = output / "server.log"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    report = {"mode": args.mode, "base_url": base, "endpoint": "POST /bank-card/review",
              "workers": 1, "ocr_device": args.device.upper() if args.mode == "paddle" else "mock",
              "ai_assist_enabled": False, "load_model": "closed-loop, zero think time, one outstanding request per virtual user",
              "login_and_warmup_excluded": True, "database": str(output / "review_records.db"),
              "started_at": datetime.now().astimezone().isoformat(),
              "image": {"path": str(args.image), "bytes": len(image), "sha256": hashlib.sha256(image).hexdigest()},
              "environment": {"python": platform.python_version(), "platform": platform.platform(),
                              "logical_cpus": psutil.cpu_count(), "physical_cpus": psutil.cpu_count(logical=False),
                              "memory_gb": round(psutil.virtual_memory().total / 1024**3, 1),
                              "processor": os.getenv("PROCESSOR_IDENTIFIER", "unknown"),
                              "packages": {dist.metadata["Name"]: dist.version for dist in distributions()
                                           if (dist.metadata["Name"] or "").lower() in
                                           {"fastapi", "uvicorn", "httpx", "opencv-python", "paddleocr", "paddlepaddle", "paddlepaddle-gpu"}}},
              "code_sha256": {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sorted((ROOT / "app").glob("*.py"))}, "stages": []}
    if args.device == "gpu":
        report["gpu_dll_directories"] = [str(p) for p in sorted({dll.parent for dll in
            (Path(sysconfig.get_path("purelib")) / "nvidia").rglob("*.dll")})]
    username, password = "load-test-only", secrets.token_urlsafe(24)
    seed = "from app.users import create_user; import os; create_user(username='load-test-only',password=os.environ['LOAD_TEST_PASSWORD'],role='admin')"
    subprocess.run([sys.executable, "-c", seed], cwd=ROOT, env={**env, "LOAD_TEST_PASSWORD": password},
                   check=True, capture_output=True, creationflags=flags)
    process = None
    with log_path.open("w", encoding="utf-8") as log:
        try:
            if args.device == "gpu":
                probe_code = (
                    "import paddle,json; from paddlex.utils.device import get_default_device; "
                    "paddle.set_device('gpu:0'); x=paddle.ones([2,2]); y=paddle.matmul(x,x); "
                    "print(json.dumps({'default_ocr_device':get_default_device(),"
                    "'tensor_place':str(y.place),'gpu_compute_sum':float(y.sum().numpy())}))"
                )
                probe = subprocess.run([sys.executable, "-c", probe_code], cwd=ROOT, env=env,
                                       capture_output=True, timeout=45, creationflags=flags)
                if probe.returncode:
                    log.write(probe.stderr.decode("utf-8", errors="replace"))
                    raise RuntimeError("GPU computation preflight failed; inspect server.log")
                report["gpu_preflight"] = json.loads(probe.stdout.decode("utf-8").strip().splitlines()[-1])
                if report["gpu_preflight"]["default_ocr_device"] != "gpu:0":
                    raise RuntimeError("OCR default device is not GPU; refuse mislabeled benchmark")
                print(json.dumps(report["gpu_preflight"]), flush=True)
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                                        "--port", str(port), "--workers", "1", "--log-level", "warning", "--no-access-log"],
                                       cwd=ROOT, env=env, stdout=log, stderr=log, creationflags=flags)
            report["server_pid"] = process.pid
            async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=180) as client:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError("Server exited before readiness; inspect server.log")
                    try:
                        response = await client.get("/csrf-token", timeout=1)
                        response.raise_for_status()
                        break
                    except httpx.HTTPError:
                        await asyncio.sleep(0.2)
                else:
                    raise RuntimeError("Server readiness timed out")
                token = response.json()["csrf_token"]
                response = await client.post("/login", data={"username": username, "password": password},
                                              headers={"X-CSRF-Token": token})
                if response.status_code != 303 or response.headers.get("location") != "/admin/reviews":
                    raise RuntimeError("Authentication failed")
                token = (await client.get("/csrf-token")).json()["csrf_token"]
                warmed = time.perf_counter()
                response = await client.post("/bank-card/review", files={"file": ("synthetic-load.png", image, "image/png")},
                                              headers={"X-CSRF-Token": token})
                report["cold_request_s"] = round(time.perf_counter() - warmed, 3)
                if response.status_code != 200:
                    report["warmup_error"] = {"status": response.status_code, "body": response.text[:500]}
                    raise RuntimeError("Warmup failed; inspect server.log")
                body = response.json()
                expected = {key: body[key] for key in ("review_result", "review_reasons", "fields", "ocr_text")}
                report["warmup_review_result"] = body["review_result"]
                request_ids = {body["request_id"]}
                server = psutil.Process(process.pid)
                for concurrency in args.concurrency:
                    if process.poll() is not None:
                        raise RuntimeError(f"Server exited during load: exit_code={process.returncode}")
                    print(f"stage start mode={args.mode} concurrency={concurrency}", flush=True)
                    result = await stage(base, client.cookies, token, image, concurrency, args.seconds, server, expected, request_ids)
                    report["stages"].append(result)
                    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                    if process.poll() is not None:
                        raise RuntimeError(f"Server exited during load: exit_code={process.returncode}")
                with sqlite3.connect(output / "review_records.db") as connection:
                    rows = connection.execute("SELECT request_id FROM review_records").fetchall()
                persisted = {row[0] for row in rows}
                report["audit_check"] = {"rows": len(rows), "acknowledged_unique_requests": len(request_ids),
                                         "missing_acknowledged_records": len(request_ids - persisted),
                                         "extra_records": len(persisted - request_ids)}
                report["status"] = "completed"
        except Exception as exc:
            report["status"] = "failed"
            report["error"] = {"type": type(exc).__name__, "message": str(exc)}
            print(json.dumps(report["error"], ensure_ascii=False), flush=True)
        finally:
            report["terminated_by_harness"] = bool(process and process.poll() is None)
            report["exit_before_cleanup"] = process.poll() if process else None
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            report["server_stopped"] = process is None or process.poll() is not None
            report["server_exit_code"] = process.poll() if process else None
            report["ended_at"] = datetime.now().astimezone().isoformat()
            (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["mock", "paddle"], required=True)
    parser.add_argument("--device", choices=["cpu", "gpu"], default="cpu")
    parser.add_argument("--concurrency", type=int, nargs="+", required=True)
    parser.add_argument("--seconds", type=int, default=15)
    parser.add_argument("--image", type=Path, default=ROOT / "data/processed/bank_card/normal/bank_card_0001.png")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 60 or not all(1 <= value <= 100 for value in args.concurrency):
        parser.error("Bounded local test requires 1-60 seconds per stage and concurrency 1-100")
    if args.device == "gpu" and args.mode != "paddle":
        parser.error("GPU benchmarking requires --mode paddle")
    output = ROOT / "reports/test-artifacts/pressure" / f"{datetime.now():%Y%m%d-%H%M%S}-{args.mode}-{args.device}"
    output.mkdir(parents=True, exist_ok=False)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("LLM_", "OPENAI_", "ANTHROPIC_", "DEEPSEEK_"))}
    env.update(REVIEW_RECORDS_DB_PATH=str(output / "review_records.db"), OCR_MODE=args.mode,
               SESSION_SECRET=secrets.token_urlsafe(32), LLM_PROVIDER="none", AI_ASSIST_ENABLED="false",
               CUDA_VISIBLE_DEVICES="0" if args.device == "gpu" else "-1",
               PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True", PYTHONUTF8="1", PYTHONFAULTHANDLER="1")
    if args.device == "gpu" and os.name == "nt":
        dll_dirs = sorted({str(dll.parent) for dll in
                           (Path(sysconfig.get_path("purelib")) / "nvidia").rglob("*.dll")})
        env["PATH"] = os.pathsep.join(dll_dirs + [env.get("PATH", "")])
    report = asyncio.run(benchmark(args, output, env, args.image.read_bytes()))
    print(f"REPORT={output / 'report.json'}", flush=True)
    raise SystemExit(0 if report["status"] == "completed" else 1)


if __name__ == "__main__":
    main()
