# =============================================================================
# Noble Trader / Kairos — single-box container image
#
# Builds the FULL five-process stack. start.sh launches all five inside one
# container; docker-compose.yml is the orchestration layer that mounts live
# state (config/, .cache/) and publishes only the gateway port.
#
#   docker build -t kairos:latest .
#   docker compose up -d
#
# Base image: python:3.11-slim-bookworm
#   - 3.11 matches requires-python >= 3.11 in pyproject.toml
#   - slim keeps the image small; hmmlearn needs a C toolchain at build time
#   - torch is the dominant layer (~2 GB) — it is NOT cached between code
#     changes unless you use the docker build cache. See the note at the
#     bottom of this file.
# =============================================================================

FROM python:3.11-slim-bookworm

# -----------------------------------------------------------------------------
# Build-time args
# -----------------------------------------------------------------------------
ARG TORCH_VERSION=2.4.0
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

# -----------------------------------------------------------------------------
# System dependencies
#   build-essential  — hmmlearn is a C extension, compiles at install time
#   curl             — healthchecks + start.sh's wait_for_http
#   libgomp1         — OpenMP runtime (numpy/scipy/torch)
#   ca-certificates  — outbound HTTPS to Supabase / TradingView / Alpaca / Discord
# -----------------------------------------------------------------------------
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        libgomp1 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------------------
# Python environment
#   - Keep the venv out of the repo (PYTHONPATH is set explicitly below)
#   - PIP_NO_CACHE_DIR keeps the layer small
# -----------------------------------------------------------------------------
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# -----------------------------------------------------------------------------
# Dependencies first — this layer only rebuilds when requirements.txt changes.
# torch is installed from the CPU wheel index: the stack runs CPU-only today
# (renko/ has no torch at all; TimesFM uses cuda only if a GPU is present).
# -----------------------------------------------------------------------------
COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install --index-url "${TORCH_INDEX_URL}" "torch>=2.4.0" \
    && pip install -r requirements.txt

# -----------------------------------------------------------------------------
# Application code
# -----------------------------------------------------------------------------
COPY . .

# -----------------------------------------------------------------------------
# Runtime environment
#   PYTHONPATH — the repo has no top-level src/; all Python sits at repo root
#   HF_HOME    — model cache lives on a mounted volume so restarts and
#                redeploys do not re-download ~4 GB of weights
#   PUBLIC_PORT — the only port that should be published
# -----------------------------------------------------------------------------
ENV PYTHONPATH=/app \
    HF_HOME=/data/hf_cache \
    PUBLIC_PORT=8000 \
    TIMESFM_READY_TIMEOUT=300

# -----------------------------------------------------------------------------
# Live state — these MUST be volumes, never baked into the image.
#
#   config/    feature_flags.yaml is the runtime authority (see repo guide §7).
#              A container image must never ship a stale copy of it.
#   .cache/    HF weights + regime_state.json (restart-safe regime state).
#   .env.local secrets — mounted read-only from the host, never COPY'd.
# -----------------------------------------------------------------------------
VOLUME ["/data", "/app/config", "/app/.cache"]

EXPOSE 8000

# -----------------------------------------------------------------------------
# Healthcheck — the gateway is the only public port, so it is the only thing
# worth probing. The gateway aggregates both backends (proxy.py /health), so a
# 200 here means main + TimesFM are both up.
#   NOTE: this is a process check, NOT a liveness check. It cannot see a
#   silent stall (engine up, producing nothing) — that is what
#   scripts/watchdog_liveness.py is for, and it must run OFF the box.
# -----------------------------------------------------------------------------
HEALTHCHECK --interval=60s --timeout=10s --start-period=300s --retries=3 \
    CMD curl -sf http://127.0.0.1:8000/health || exit 1

# -----------------------------------------------------------------------------
# Entrypoint
#   start.sh already implements the correct start ORDER (TimesFM first, wait
#   for model load, then main, then orchestrator, then gateway). Reusing it
#   keeps the container behaviour identical to the bare-metal deployment —
#   do not reimplement that ordering here.
#
#   start.sh runs `wait` at the end, so it stays in the foreground and Docker
#   can manage its lifecycle. It traps SIGTERM and kills all children.
# -----------------------------------------------------------------------------
CMD ["bash", "start.sh"]
