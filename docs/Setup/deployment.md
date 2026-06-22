# Deployment Guide

Operational reference for running the Dynamic Regime Risk Management Platform in production.

---

## Running Locally

```bash
# Development (single worker, auto-reload)
uvicorn main:app --reload --port 8000

# Production-like (4 workers, no reload)
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
```

> **Note on multiple workers:** Each uvicorn worker process has its own `SessionRegistry`. Live streaming sessions are not shared across workers. For multi-worker deployments with streaming, use a single worker or add Redis-backed session storage (see Extending below).

---

## Docker

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```bash
docker build -t regime-platform .
docker run -p 8000:8000 regime-platform
```

---

## Environment Variables

### Server

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8000` | Listening port |
| `WORKERS` | `1` | Uvicorn worker count |
| `LOG_LEVEL` | `info` | Uvicorn log level: `debug` \| `info` \| `warning` \| `error` |

### Authentication

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTH_ENABLED` | `true` | Enable authentication |
| `JWT_SECRET_KEY` | `""` | Secret for JWT signing (required for auth) |
| `API_KEYS` | `""` | Comma-separated static API keys |
| `CLERK_PUBLISHABLE_KEY` | `""` | Clerk publishable key (frontend-only, not used by backend) |
| `CLERK_SECRET_KEY` | `""` | Clerk secret key (required for Clerk JWT verification) |
| `CLERK_JWKS_URL` | `""` | Clerk JWKS URL (auto-resolved from JWT issuer if empty) |

### Supabase (REQUIRED for runtime_config + pipeline persistence)

| Variable | Default | Description |
|----------|---------|-------------|
| `SUPABASE_URL` | `""` | Supabase project URL (e.g. `https://pcvscowltlrxzgxjurcr.supabase.co`) |
| `SUPABASE_KEY` | `""` | Supabase **service_role** key (NOT the anon/publishable key) |

> **CRITICAL - Required on Render**: Both `SUPABASE_URL` and `SUPABASE_KEY` must be set
> in the Render environment. If either is missing, the Renko pipeline endpoints will return
> HTTP 500 because `runtime_config` cannot initialise and `RenkoConfig.from_runtime_config()`
> will fail. The `/renko/health` diagnostic endpoint can be used to verify connectivity.
>
> **There is NO `SUPABASE_PUBLISHABLE_KEY` variable on the backend.** The backend requires
> the **service_role** key because it needs full database access for `nt_system_config`,
> `ta_renko_snapshot`, `api_keys`, `nt_system_config_audit`, and `rate_limit_violations` tables.
> The anon/publishable key (`NEXT_PUBLIC_SUPABASE_ANON_KEY`) is for the **frontend only**
> (Next.js on Vercel) and does NOT have sufficient permissions for backend operations.
>
> **Summary of Supabase keys across the stack:**
>
> | Component | Env Var | Key Type | Purpose |
> |-----------|---------|----------|--------|
> | Backend (Render) | `SUPABASE_KEY` | service_role | Full DB access for config, snapshots, auth |
> | Frontend (Vercel) | `NEXT_PUBLIC_SUPABASE_URL` | - | Supabase URL |
> | Frontend (Vercel) | `NEXT_PUBLIC_SUPABASE_ANON_KEY` | anon/public | Client-side RLS-protected access |

### Trading

| Variable | Default | Description |
|----------|---------|-------------|
| `ALPACA_API_KEY` | `""` | Alpaca paper/live API key |
| `ALPACA_SECRET_KEY` | `""` | Alpaca secret key |
| `ALPACA_BASE_URL` | `https://paper-api.alpaca.markets` | Alpaca base URL |

### Notifications

| Variable | Default | Description |
|----------|---------|-------------|
| `DISCORD_WEBHOOK_SIGNALS` | `""` | Discord webhook for trade signals |
| `DISCORD_WEBHOOK_EXECUTIONS` | `""` | Discord webhook for trade executions |
| `DISCORD_WEBHOOK_STATUS` | `""` | Discord webhook for status updates |
| `TELEGRAM_BOT_TOKEN` | `""` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | `""` | Telegram chat ID |

### Caching

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | `""` | Redis URL for pipeline state persistence |
| `CORS_ORIGINS` | `http://localhost:3000` | Comma-separated allowed origins |

---

## Performance Characteristics

| Metric | Value | Notes |
|--------|-------|-------|
| Tick latency (p50) | ~7 ms | HMM predict + Kelly + VaR/CVaR |
| HMM refit latency | 200–500 ms | Background, non-blocking |
| Max concurrent sessions | ~500 | Per worker, memory-bound |
| Price buffer memory | ~32 KB | Per session at window=500 |
| SSE fan-out overhead | ~0.1 ms/subscriber | Queue put_nowait |

---

## Health Check

```bash
# Main health check
curl http://localhost:8000/health
# {"status": "ok", "version": "7.0.0"}

# Renko pipeline health check (no auth required)
curl http://localhost:8000/renko/health
# {"status": "ok", "runtime_config": {"db_available": true}, ...}
```

---

## Extending

### Redis-backed session persistence

Replace the in-memory `deque` in `StreamSession` with a Redis list:

```python
import redis.asyncio as redis

r = redis.from_url("redis://localhost:6379")

async def _append_price(self, price: float):
    key = f"prices:{self.symbol}"
    await r.rpush(key, price)
    await r.ltrim(key, -self.window, -1)  # keep last N

async def _load_prices(self) -> list[float]:
    raw = await r.lrange(f"prices:{self.symbol}", 0, -1)
    return [float(x) for x in raw]
```

### GPU-accelerated HMM

Replace `hmmlearn.hmm.GaussianHMM` with `pomegranate`:

```python
from pomegranate.hmm import DenseHMM
from pomegranate.distributions import Normal

model = DenseHMM([Normal() for _ in range(3)])
model.fit(sequences)  # GPU via PyTorch backend
```

### Kafka consumer integration

```python
from aiokafka import AIOKafkaConsumer

async def consume_kafka(topic: str, symbol: str):
    consumer = AIOKafkaConsumer(topic, bootstrap_servers="localhost:9092")
    await consumer.start()
    session = await registry.get_or_create(symbol)
    async for msg in consumer:
        price = float(msg.value)
        await session.tick(price)
```

### Authentication (JWT on WebSocket)

```python
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.security import HTTPBearer

@router.websocket("/ws/{symbol}")
async def websocket_stream(websocket: WebSocket, symbol: str, token: str = Query(...)):
    payload = verify_jwt(token)  # raises if invalid
    await websocket.accept()
    ...
```

---

## Monitoring

Key metrics to track in production:

| Metric | Alert threshold | Description |
|--------|-----------------|-------------|
| `tick_latency_p95` | > 100ms | Slow HMM predict path |
| `refit_latency_p95` | > 2000ms | HMM EM fitting taking too long |
| `session_count` | > 400 | Approaching per-worker memory limit |
| `alert_queue_depth` | > 500 | Alert broadcast queue backing up |
| `regime_change_rate` | > 10/hour | Possible noisy data or too-short window |

---

## HMM Tuning

| Scenario | Recommendation |
|----------|----------------|
| Short seed (< 150 bars) | Use `covariance_type="diag"`, `n_iter=50` |
| Long seed (≥ 500 bars) | Can use `covariance_type="full"`, `n_iter=150` |
| Slow regime convergence | Increase `refit_every` to 100+ bars |
| Too many false alerts | Increase debounce from 3 to 5 bars in `stream_session.py` |
| High-frequency data | Reduce `window` to 200; increase `refit_every` to 100 |
