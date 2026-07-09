#!/usr/bin/env python3
"""
Noble Trader Stack — Background Execution (Lightning AI "play" button)

Starts four services as subprocesses and waits for them:
  1. TimesFM server   (port 8002) — started first (slowest: 30-60s model load)
  2. Main app          (port 8001)
  3. Gateway proxy     (port 8000) — public-facing
  4. Sweep orchestrator (port 8003) — started last, after TimesFM is ready

Use this as the Lightning AI "play" button entry point.

Logging:
  Each service's stdout+stderr is written to BOTH the terminal (if running
  interactively) AND a log file in logs/:
    logs/timesfm.log       — TimesFM server (port 8002)
    logs/main.log          — Main app (port 8001)
    logs/gateway.log       — Gateway/proxy (port 8000)
    logs/orchestrator.log  — Sweep orchestrator (port 8003)
    logs/startup.log       — this script's own output

  View logs:
    tail -f logs/orchestrator.log    # orchestrator only
    tail -f logs/*.log               # all logs
    ./lightningstudio/logs.sh        # helper script

Fixes from previous version:
  - Added missing gateway/proxy.py (was only starting 3 services)
  - Set PORT=8001 for main app explicitly (was conflicting with gateway)
  - Start TimesFM first (slowest — model download + load)
  - Wait for TimesFM /health status="ok" before starting orchestrator
  - Fixed wait loop: uses any() instead of all() (keeps running while
    at least one service is alive)
  - Added file-based logging (in addition to terminal output) so logs
    survive even when no terminal is attached
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)

# ── Config ─────────────────────────────────────────────────────────────────
TIMESFM_READY_TIMEOUT = int(os.environ.get("TIMESFM_READY_TIMEOUT", "180"))

# ── Setup log directory ───────────────────────────────────────────────────
# Each service gets its own log file. Logs are appended to (not overwritten)
# so history survives restarts.
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

LOG_TIMESFM = os.path.join(LOG_DIR, "timesfm.log")
LOG_MAIN = os.path.join(LOG_DIR, "main.log")
LOG_GATEWAY = os.path.join(LOG_DIR, "gateway.log")
LOG_ORCH = os.path.join(LOG_DIR, "orchestrator.log")
LOG_STARTUP = os.path.join(LOG_DIR, "startup.log")


def log(msg=""):
    """Print a message to both stdout and the startup log file."""
    print(msg)
    with open(LOG_STARTUP, "a") as f:
        f.write(msg + "\n")
        f.flush()


# ── Subprocess output tee (correct approach) ──────────────────────────────
# subprocess.Popen does NOT call the stdout/stderr object's write() method.
# It uses fileno() to get a file descriptor, then the child process writes
# directly to that fd via dup2(). So passing a custom TeeWriter object to
# Popen(stdout=...) does NOT work — the child writes to the fd (terminal)
# and the TeeWriter.write() method is never called, so the log file stays
# empty.
#
# The correct approach: use stdout=PIPE + stderr=STDOUT, then start a
# daemon thread that reads lines from the pipe and writes them to BOTH
# sys.stdout (terminal) AND the log file. This is the standard pattern
# for teeing subprocess output in Python.


_output_threads = []  # keep refs so threads aren't GC'd


def _pipe_reader_thread(proc: subprocess.Popen, log_file, label: str):
    """Read lines from proc.stdout and write to terminal + log file.

    Runs as a daemon thread — exits automatically when the main process
    exits or when proc.stdout reaches EOF (child process closed stdout).
    """
    try:
        # With text=True, readline returns str. When the pipe closes,
        # readline returns "" (empty string) — that's our EOF signal.
        for line in proc.stdout:
            if not line:
                break
            # Write to terminal
            try:
                sys.stdout.write(line)
                sys.stdout.flush()
            except Exception:
                pass  # don't let a broken pipe kill the thread
            # Write to log file
            try:
                log_file.write(line)
                log_file.flush()
            except Exception:
                pass
    except Exception:
        pass  # thread exits silently on any error


processes = []
log_files = []  # keep refs so files don't get GC'd


def cleanup(signum=None, frame=None):
    log("\nShutting down all services...")
    for proc in processes:
        if proc.poll() is None:
            proc.terminate()
    for proc in processes:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    # Give pipe reader threads a moment to flush remaining output
    time.sleep(0.5)
    # Close log files
    for f in log_files:
        try:
            f.close()
        except Exception:
            pass
    log("All services stopped.")
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
            log(f"  ✓ {label} is up (after {elapsed}s)")
            return True
        except Exception:
            time.sleep(2)
            elapsed += 2
    log(f"  ⚠ {label} not responding after {timeout}s")
    return False


def wait_for_timesfm_ready(pid, timeout=TIMESFM_READY_TIMEOUT):
    """Wait for TimesFM model to finish loading.

    server.py's /health returns {"status":"loading"} while the model is
    loading and {"status":"ok"} once ready. We must check the JSON body,
    not just the HTTP status code (which is 200 in both cases).
    """
    elapsed = 0
    log(f"  Waiting for TimesFM model to load (timeout: {timeout}s)...")
    log(f"  → Watch TimesFM load progress: tail -f {LOG_TIMESFM}")
    while elapsed < timeout:
        # Check if the process is still alive
        try:
            os.kill(pid, 0)  # signal 0 = check if process exists
        except ProcessLookupError:
            log("  ✗ TimesFM process died during model load!")
            log(f"  → Check {LOG_TIMESFM} for the error")
            return False

        health = _http_get_json("http://127.0.0.1:8002/health")
        if health:
            status = health.get("status", "")
            if status == "ok":
                log(f"  ✓ TimesFM model loaded (after {elapsed}s)")
                return True
            # Still loading — show periodic progress
            if elapsed > 0 and elapsed % 10 == 0:
                log(f"    ...still loading ({elapsed}s elapsed)")

        time.sleep(3)
        elapsed += 3

    log(f"  ⚠ TimesFM model not ready after {timeout}s — "
        f"starting orchestrator anyway (will retry on each sweep)")
    return False


def start_service(name, cmd, env, log_path, port):
    """Start a service subprocess with output tee'd to terminal + log file.

    Uses stdout=PIPE + stderr=STDOUT + a daemon reader thread to write
    output to BOTH the terminal (sys.stdout) AND the log file. This is
    the correct way to tee subprocess output in Python — passing a custom
    file-like object to Popen does NOT work because Popen uses fileno()
    + dup2(), not the object's write() method.

    Returns the Popen object.
    """
    log_file = open(log_path, "a", buffering=1)  # line-buffered
    log_files.append(log_file)

    log(f"\n  Starting {name} on port {port}...")
    log(f"    Log: {log_path}")

    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # merge stderr into stdout so both go to the same pipe
        text=True,                # readline returns str, not bytes
        bufsize=1,                # line-buffered
    )
    processes.append(proc)

    # Start a daemon thread to read from the pipe and write to terminal + log
    thread = threading.Thread(
        target=_pipe_reader_thread,
        args=(proc, log_file, name),
        name=f"pipe-{name}",
        daemon=True,
    )
    thread.start()
    _output_threads.append(thread)

    log(f"    PID: {proc.pid}")
    return proc


# ── Main ───────────────────────────────────────────────────────────────────

log("═══════════════════════════════════════════════════════════════════")
log(f"  Noble Trader — Background Stack")
log(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
log(f"  Starting 4 services:")
log(f"    TimesFM     → port 8002 (started first — slow model load)")
log(f"    Main app    → port 8001")
log(f"    Gateway     → port 8000 (public-facing)")
log(f"    Sweep Orch  → port 8003 (started last, after TimesFM ready)")
log(f"  Logs: {LOG_DIR}/")
log("═══════════════════════════════════════════════════════════════════")

# ── 1. Start TimesFM FIRST (slowest — model download + load) ──────────────
log("\n[1/4] Starting TimesFM server on port 8002...")
timesfm_env = os.environ.copy()
timesfm_env["PORT"] = "8002"
timesfm_proc = start_service(
    "TimesFM",
    [sys.executable, "server.py"],
    timesfm_env,
    LOG_TIMESFM,
    8002,
)
log("  (model load happens in background — will wait for it below)")

# ── 2. Start Main App on port 8001 ────────────────────────────────────────
# CRITICAL: set PORT=8001 explicitly. The previous version didn't set PORT,
# so main.py defaulted to 8000 — conflicting with the gateway (proxy.py)
# that also binds to 8000.
log("\n[2/4] Starting Main App on port 8001...")
main_env = os.environ.copy()
main_env["PORT"] = "8001"
main_env["RATE_LIMIT_COMPUTE"] = "30/minute"
main_proc = start_service(
    "Main App",
    [sys.executable, "main.py"],
    main_env,
    LOG_MAIN,
    8001,
)

# Wait for main app to bind
wait_for_http("http://127.0.0.1:8001/health", "Main App", timeout=30)

# ── 3. Start Gateway Proxy on port 8000 ───────────────────────────────────
# The previous version was MISSING this service entirely — only started 3
# services (main, timesfm, orch), so the public URL on port 8000 had
# nothing listening. Now we start proxy.py which routes:
#   /forecast* → TimesFM (port 8002)
#   /*         → Main app (port 8001)
log("\n[3/4] Starting Gateway Proxy on port 8000...")
gateway_env = os.environ.copy()
gateway_env["PORT"] = "8000"
gateway_proc = start_service(
    "Gateway",
    [sys.executable, "proxy.py"],
    gateway_env,
    LOG_GATEWAY,
    8000,
)

# ── 4. Wait for TimesFM model to load, then start orchestrator ────────────
log("\n[4/4] Waiting for TimesFM model before starting orchestrator...")
wait_for_timesfm_ready(timesfm_proc.pid, timeout=TIMESFM_READY_TIMEOUT)

# Now start the orchestrator — TimesFM should be ready (or we timed out
# and the orchestrator will retry on its own).
log("\n  Starting Sweep Orchestrator on port 8003...")
orch_env = os.environ.copy()
orch_env["SWEEP_ORCHESTRATOR_PORT"] = "8003"
orch_env["PYTHONUNBUFFERED"] = "1"
orch_proc = start_service(
    "Sweep Orchestrator",
    [sys.executable, "-m", "regime_platform.sweep_orchestrator"],
    orch_env,
    LOG_ORCH,
    8003,
)

# ── Final health summary ──────────────────────────────────────────────────
log("\n═══════════════════════════════════════════════════════════════════")
log("  All 4 services started. Health summary:")
main_h = _http_get_json("http://127.0.0.1:8001/health")
log(f"    Main App    : {main_h.get('status', '?') if main_h else 'DOWN'}")
tfm_h = _http_get_json("http://127.0.0.1:8002/health")
log(f"    TimesFM     : {tfm_h.get('status', '?') if tfm_h else 'DOWN'}")
gw_h = _http_get_json("http://127.0.0.1:8000/health")
log(f"    Gateway     : {gw_h.get('status', '?') if gw_h else 'DOWN'}")
log("")

public_id = os.environ.get("LIGHTNING_INSTANCE_ID", "")
log(f"  Public URL: https://8000-{public_id}.cloudspaces.litng.ai")
log("")
log("  ── How to view logs ──────────────────────────────────────────────")
log(f"  All logs:     tail -f {LOG_DIR}/*.log")
log(f"  Orchestrator: tail -f {LOG_ORCH}")
log(f"  TimesFM:      tail -f {LOG_TIMESFM}")
log(f"  Main app:     tail -f {LOG_MAIN}")
log(f"  Gateway:      tail -f {LOG_GATEWAY}")
log(f"  Startup:      tail -f {LOG_STARTUP}")
log(f"  Helper:       ./lightningstudio/logs.sh [all|orchestrator|timesfm|main|gateway]")
log("═══════════════════════════════════════════════════════════════════")

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
            log(f"Process PID {proc.pid} exited with code {rc}")
    cleanup()
except KeyboardInterrupt:
    cleanup()
