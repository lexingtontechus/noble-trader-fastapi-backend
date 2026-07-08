#!/usr/bin/env python3
"""
Noble Trader Stack — Background Execution (Lightning AI "play" button)

Starts four services as subprocesses and waits for them:
  1. TimesFM server   (port 8002) — started first (slowest: 30-60s model load)
  2. Main app          (port 8001)
  3. Gateway proxy     (port 8000) — public-facing
  4. Sweep orchestrator (port 8003) — started last, after TimesFM is ready

Use this as the Lightning AI "play" button entry point.

Fixes from previous version:
  - Added missing gateway/proxy.py (was only starting 3 services, so the
    public URL on port 8000 had nothing listening)
  - Set PORT=8001 for main app explicitly (was defaulting to 8000,
    conflicting with the gateway)
  - Start TimesFM first (it's the slowest — model download + load can
    take 2-3 min on first run)
  - Wait for TimesFM /health to return status="ok" (model loaded) before
    starting the orchestrator, preventing the first sweep from getting a
    503 and falling into "TimesFM unavailable" fallback mode
  - Fixed the wait loop: now uses `any()` instead of `all()` so the
    script keeps running as long as at least one service is alive
    (previously exited as soon as any single service crashed)
"""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)

# ── Config ─────────────────────────────────────────────────────────────────
# How long to wait for TimesFM model to load before starting the orchestrator
# anyway (it'll retry on its own). Default: 180s.
TIMESFM_READY_TIMEOUT = int(os.environ.get("TIMESFM_READY_TIMEOUT", "180"))

processes = []


def cleanup(signum=None, frame=None):
    print("\nShutting down all services...")
    for proc in processes:
        if proc.poll() is None:
            proc.terminate()
    for proc in processes:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("All services stopped.")
    sys.exit(0)


signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)


# ── Helpers ────────────────────────────────────────────────────────────────

def _http_get_json(url, timeout=3):
    """Fetch JSON from a URL. Returns parsed dict or None on failure."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def wait_for_http(url, label, timeout=30):
    """Wait for URL to return HTTP 200. Returns True on success."""
    elapsed = 0
    while elapsed < timeout:
        try:
            urllib.request.urlopen(url, timeout=2)
            print(f"  ✓ {label} is up (after {elapsed}s)")
            return True
        except Exception:
            time.sleep(2)
            elapsed += 2
    print(f"  ⚠ {label} not responding after {timeout}s")
    return False


def wait_for_timesfm_ready(pid, timeout=TIMESFM_READY_TIMEOUT):
    """Wait for TimesFM model to finish loading.

    server.py's /health returns {"status":"loading"} while the model is
    loading and {"status":"ok"} once ready. We must check the JSON body,
    not just the HTTP status code (which is 200 in both cases).
    """
    elapsed = 0
    print(f"  Waiting for TimesFM model to load (timeout: {timeout}s)...")
    while elapsed < timeout:
        # Check if the process is still alive
        try:
            os.kill(pid, 0)  # signal 0 = check if process exists
        except ProcessLookupError:
            print("  ✗ TimesFM process died during model load!")
            return False

        health = _http_get_json("http://127.0.0.1:8002/health")
        if health:
            status = health.get("status", "")
            if status == "ok":
                print(f"  ✓ TimesFM model loaded (after {elapsed}s)")
                return True
            # Still loading — show periodic progress
            if elapsed > 0 and elapsed % 10 == 0:
                print(f"    ...still loading ({elapsed}s elapsed)")

        time.sleep(3)
        elapsed += 3

    print(f"  ⚠ TimesFM model not ready after {timeout}s — "
          f"starting orchestrator anyway (will retry on each sweep)")
    return False


# ── Main ───────────────────────────────────────────────────────────────────

print("═══════════════════════════════════════════════════════════════════")
print("  Noble Trader — Background Stack (Lightning AI play button)")
print("  Starting 4 services:")
print("    TimesFM     → port 8002 (started first — slow model load)")
print("    Main app    → port 8001")
print("    Gateway     → port 8000 (public-facing)")
print("    Sweep Orch  → port 8003 (started last, after TimesFM ready)")
print("═══════════════════════════════════════════════════════════════════")

# ── 1. Start TimesFM FIRST (slowest — model download + load) ──────────────
print("\n[1/4] Starting TimesFM server on port 8002...")
timesfm_env = os.environ.copy()
timesfm_env["PORT"] = "8002"
timesfm_proc = subprocess.Popen(
    [sys.executable, "server.py"],
    env=timesfm_env,
    stdout=sys.stdout,
    stderr=sys.stderr,
)
processes.append(timesfm_proc)
print(f"  PID timesfm: {timesfm_proc.pid}")
print("  (model load happens in background — will wait for it below)")

# ── 2. Start Main App on port 8001 ────────────────────────────────────────
# CRITICAL: set PORT=8001 explicitly. The previous version didn't set PORT,
# so main.py defaulted to 8000 — conflicting with the gateway (proxy.py)
# that also binds to 8000.
print("\n[2/4] Starting Main App on port 8001...")
main_env = os.environ.copy()
main_env["PORT"] = "8001"
main_env["RATE_LIMIT_COMPUTE"] = "30/minute"
main_proc = subprocess.Popen(
    [sys.executable, "main.py"],
    env=main_env,
    stdout=sys.stdout,
    stderr=sys.stderr,
)
processes.append(main_proc)
print(f"  PID main: {main_proc.pid}")

# Wait for main app to bind
wait_for_http("http://127.0.0.1:8001/health", "Main App", timeout=30)

# ── 3. Start Gateway Proxy on port 8000 ───────────────────────────────────
# The previous version was MISSING this service entirely — only started 3
# services (main, timesfm, orch), so the public URL on port 8000 had
# nothing listening. Now we start proxy.py which routes:
#   /forecast* → TimesFM (port 8002)
#   /*         → Main app (port 8001)
print("\n[3/4] Starting Gateway Proxy on port 8000...")
gateway_env = os.environ.copy()
gateway_env["PORT"] = "8000"
gateway_proc = subprocess.Popen(
    [sys.executable, "proxy.py"],
    env=gateway_env,
    stdout=sys.stdout,
    stderr=sys.stderr,
)
processes.append(gateway_proc)
print(f"  PID gateway: {gateway_proc.pid}")

# ── 4. Wait for TimesFM model to load, then start orchestrator ────────────
print("\n[4/4] Waiting for TimesFM model before starting orchestrator...")
wait_for_timesfm_ready(timesfm_proc.pid, timeout=TIMESFM_READY_TIMEOUT)

# Now start the orchestrator — TimesFM should be ready (or we timed out
# and the orchestrator will retry on its own).
print("\n  Starting Sweep Orchestrator on port 8003...")
orch_env = os.environ.copy()
orch_env["SWEEP_ORCHESTRATOR_PORT"] = "8003"
orch_env["PYTHONUNBUFFERED"] = "1"
orch_proc = subprocess.Popen(
    [sys.executable, "-m", "regime_platform.sweep_orchestrator"],
    env=orch_env,
    stdout=sys.stdout,
    stderr=sys.stderr,
)
processes.append(orch_proc)
print(f"  PID orch: {orch_proc.pid}")

# ── Final health summary ──────────────────────────────────────────────────
print("\n═══════════════════════════════════════════════════════════════════")
print("  All 4 services started. Health summary:")
main_h = _http_get_json("http://127.0.0.1:8001/health")
print(f"    Main App    : {main_h.get('status', '?') if main_h else 'DOWN'}")
tfm_h = _http_get_json("http://127.0.0.1:8002/health")
print(f"    TimesFM     : {tfm_h.get('status', '?') if tfm_h else 'DOWN'}")
gw_h = _http_get_json("http://127.0.0.1:8000/health")
print(f"    Gateway     : {gw_h.get('status', '?') if gw_h else 'DOWN'}")
print()

public_id = os.environ.get("LIGHTNING_INSTANCE_ID", "")
print(f"  Public URL: https://8000-{public_id}.cloudspaces.litng.ai")
print(f"  Press Ctrl+C to stop all services.")
print("═══════════════════════════════════════════════════════════════════")

# ── Wait loop ─────────────────────────────────────────────────────────────
# Keep running as long as at least one service is alive. The previous
# version used `all(p.poll() is None ...)` which exited as soon as ANY
# single service crashed — too aggressive. Now we use `any()` so the
# script keeps running until ALL services have exited.
try:
    while any(p.poll() is None for p in processes):
        time.sleep(5)
    # All processes exited
    for proc in processes:
        rc = proc.poll()
        if rc is not None:
            print(f"Process PID {proc.pid} exited with code {rc}")
    cleanup()
except KeyboardInterrupt:
    cleanup()
