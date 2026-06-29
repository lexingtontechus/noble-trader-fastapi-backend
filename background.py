#!/usr/bin/env python3
"""
Noble Trader Stack — Background Execution (Lightning AI "play" button)

Starts three services as subprocesses and waits for them.
Use this as the Lightning AI "play" button entry point.
"""

import os
import signal
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)

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

print("Starting Noble Trader background stack...")
print("  Main app    → will bind to PORT or fall back to 8000")
print("  TimesFM     → port 8002")
print("  Sweep Orch  → port 8003")

# Start main app
main_env = os.environ.copy()
main_env["RATE_LIMIT_COMPUTE"] = "30/minute"
main_proc = subprocess.Popen(
    [sys.executable, "main.py"],
    env=main_env,
    stdout=sys.stdout,
    stderr=sys.stderr,
)
processes.append(main_proc)
print(f"  PID main: {main_proc.pid}")

# Start TimesFM server on port 8002
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

# Start sweep orchestrator on port 8003
orch_env = os.environ.copy()
orch_env["SWEEP_ORCHESTRATOR_PORT"] = "8003"
orch_proc = subprocess.Popen(
    [sys.executable, "-m", "regime_platform.sweep_orchestrator"],
    env=orch_env,
    stdout=sys.stdout,
    stderr=sys.stderr,
)
processes.append(orch_proc)
print(f"  PID orch: {orch_proc.pid}")

print("\nAll 3 services running. Press Ctrl+C to stop.")
print(f"  Public URL: https://8000-{os.environ.get('LIGHTNING_INSTANCE_ID', '')}.cloudspaces.litng.ai")

try:
    # Wait for all processes to complete
    while all(p.poll() is None for p in processes):
        time.sleep(1)
    for proc in processes:
        rc = proc.poll()
        if rc is not None:
            print(f"Process PID {proc.pid} exited with code {rc}")
    cleanup()
except KeyboardInterrupt:
    cleanup()
