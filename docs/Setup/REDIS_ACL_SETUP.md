# Redis ACL Setup for Noble Trader Multi-Tenant Architecture

**Status:** P5 (2026-07-20) — Required for production rollout
**Audience:** Operators deploying the noble-trader-proxy + hermes agent
**Related:** `docs/Setup/REDIS_USAGE_OPTIMIZATION_ANALYSIS.md`, P3 proxy scaffold, P4 agent ACL verification

---

## Overview

Starting in P5, the Noble Trader backend uses **Redis ACL (Access Control List)** users to enforce read-only access for downstream consumers (proxy, agent). The orchestrator continues to use the full-access Redis user; consumers get a restricted user that can **only**:

- `XREADGROUP` from `signal.raw.noble_trader` (and the per-plan streams `signals:signal_scout` / `signals:precision_pro` added in P5)
- `XACK` entries they've consumed
- `XINFO` for stream/group introspection
- `PING` for health checks

This means a compromised proxy or agent **cannot** `XADD` (publish fake signals), `FLUSHDB`, `CONFIG SET`, or `EVAL` Lua scripts. The blast radius of a credential leak is reduced to "can read signals and ACK them" — which is annoying but not catastrophic.

---

## ACL User Naming Convention

| User prefix | Codebase             | Permissions                                       |
| ----------- | -------------------- | ------------------------------------------------- |
| `orch_`     | noble-trader-fastapi-backend (sweep orchestrator) | Full access (legacy default user) |
| `sub_`      | noble-trader-proxy   | `+xreadgroup +xack +xinfo +ping` on signal streams only |
| `agent_`    | noble-trader-agent   | `+xreadgroup +xack +xinfo +ping` on signal streams only |

The 32-hex suffix is a random per-deployment token (use `openssl rand -hex 16`). The suffix is NOT the password — it's part of the username to make audit logs grep-able by deployment.

---

## Railway Redis Setup

Railway's managed Redis supports ACL since Redis 6.x. The free Hobby plan supports up to 3 ACL users.

### 1. Get the orchestrator's connection string

From the Railway dashboard → your Redis service → "Connect" tab:

```
rediss://default:<password>@<host>.railway.app:<port>
```

This is the **default** user — full access. Set this as `REDIS_URL` on the noble-trader-fastapi-backend service.

### 2. Open the Redis CLI

```bash
# Railway provides a one-liner in the "Connect" tab.
# Example:
redis-cli -u rediss://default:<password>@<host>.railway.app:<port>
```

### 3. Create the proxy ACL user

```redis
ACL SETUSER sub_<32hex> on ><strong-password> ~signal.raw.noble_trader ~signals:signal_scout ~signals:precision_pro +xreadgroup +xack +xinfo +ping

# Verify
ACL GETUSER sub_<32hex>
```

Expected output:

```
1) "flags"
2) 1) "on"
3) "passwords"
4) 1) "<hashed-password>"
5) "commands"
6) "-@all +xreadgroup +xack +xinfo +ping"
7) "keys"
8) 1) "signal.raw.noble_trader"
   2) "signals:signal_scout"
   3) "signals:precision_pro"
```

### 4. Construct the proxy's connection URL

The ACL URL format follows RFC 3986 — `redis://username:password@host:port/db`:

```
rediss://sub_<32hex>:<strong-password>@<host>.railway.app:<port>/0
```

Set this as `REDIS_URL` on the noble-trader-proxy Railway service.

The proxy's `settings.py` (`P3` implementation) validates that `REDIS_URL` contains `@` (ACL credential separator) and uses the `redis://` or `rediss://` scheme. A bare `rediss://:<password>@host` (no username) will be rejected.

### 5. Create the agent ACL user

The agent uses the same permission set as the proxy (it's also a consumer):

```redis
ACL SETUSER agent_<32hex> on ><strong-password> ~signal.raw.noble_trader ~signals:signal_scout ~signals:precision_pro +xreadgroup +xack +xinfo +ping
```

Agent URL:

```
rediss://agent_<32hex>:<strong-password>@<host>.railway.app:<port>/0
```

Set this as `NOBLE_TRADER_REDIS_URL` on the hermes agent's environment (or in the setup wizard — see P4).

### 6. Persist the ACL across restarts

Railway Redis instances restart from a snapshot. To make ACL users persistent, you must:

```redis
ACL SAVE
```

This writes the current ACL to the Redis dump file. Without `ACL SAVE`, the users disappear on restart.

To verify persistence:

```bash
# Restart the Railway Redis service, then re-connect:
redis-cli -u rediss://default:<password>@<host>.railway.app:<port>
ACL LIST
# Should show both sub_<32hex> and agent_<32hex> users
```

---

## Verifying the ACL

### From the proxy container

```bash
docker exec -it <proxy-container> sh
# Inside container:
python -c "
import redis
r = redis.from_url('rediss://sub_<32hex>:<password>@host:port/0')
print(r.ping())  # True
try:
    r.xadd('signal.raw.noble_trader', {'test': '1'})
    print('FAIL: XADD succeeded — ACL not restrictive enough')
except redis.exceptions.ResponseError as e:
    print(f'OK: XADD blocked: {e}')
"
```

Expected:

```
True
OK: XADD blocked: NOPERM this user has no permissions to run the 'xadd' command
```

### From the agent

The agent's `redis_subscriber.py:201` uses `aioredis.from_url(self._redis_url)`. The URL parsing is RFC 3986 compliant — verify by running the agent with `LOG_LEVEL=DEBUG` and looking for:

```
DEBUG hermes.transport.redis_subscriber - Connected to Redis as user 'agent_<32hex>' (host=xxx.railway.app:port, db=0, ssl=True)
```

---

## Removing a Compromised ACL User

If a proxy or agent credential is leaked:

```redis
ACL DELUSER sub_<compromised-suffix>
ACL SAVE
```

Then redeploy the affected service with a new ACL user + URL. Existing consumer-group state (pending entries, etc.) is preserved — the new user just picks up where the old one left off.

---

## Troubleshooting

### `NOAUTH Authentication required`

The URL is missing credentials. Check that the URL has the `username:password@` segment.

### `WRONGPASS invalid username-password pair`

Either the password is wrong, or the username doesn't exist. Verify with `ACL GETUSER <username>` from the default user.

### `NOPERM this user has no permissions to run the 'xadd' command`

This is the **expected** error when the proxy/agent attempts to publish. It means the ACL is working correctly. If you see this in the orchestrator's logs, the orchestrator is using the wrong URL (it should use the default user, not `sub_*` / `agent_*`).

### `NOPERM this user has no permissions to run the 'xreadgroup' command`

The ACL user was created without `+xreadgroup`. Recreate with the full permission set shown in step 3 above.

### `BUSYGROUP Consumer Group name already exists`

This is benign — the proxy/agent creates its consumer group idempotently and catches this error. No action needed.

### Consumer group has pending entries from a dead consumer

If a proxy replica dies mid-message, its pending entries stay in the PEL (Pending Entries List) until claimed. To reclaim them:

```redis
XAUTOCLAIM signal.raw.noble_trader <consumer-group> <new-consumer-name> 0 0
```

This is intentionally **not** automated in P5 — operators should monitor PEL size via `/health` (`stats.pending`) and reclaim manually if it grows past 100.

---

## Migration from Pre-P5 (Default User Only)

If your existing deployment uses the default Redis user for all services:

1. **Don't** remove the default user — the orchestrator still needs it.
2. Create the `sub_*` and `agent_*` users per the steps above.
3. Update the proxy's `REDIS_URL` and agent's `NOBLE_TRADER_REDIS_URL` env vars to the new ACL URLs.
4. Redeploy both services.
5. Verify the orchestrator still publishes (its logs should show `XADD signal.raw.noble_trader` succeeding).
6. Verify the proxy/agent consume (their logs should show `XREADGROUP` returning entries).
7. After 24 hours of stable operation, you may optionally restrict the default user's permissions as well — but this is not required for P5.

---

## Related Files

| File | Purpose |
| --- | --- |
| `noble-trader-proxy/src/proxy/core/settings.py` | Validates REDIS_URL contains ACL credentials |
| `noble-trader-proxy/src/proxy/redis/subscriber.py` | `SignalSubscriber` uses XREADGROUP with `block=30000` |
| `noble-trader-agent/src/hermes/transport/redis_subscriber.py:201` | `aioredis.from_url(self._redis_url)` — parses ACL URL per RFC 3986 |
| `noble-trader-fastapi-backend/regime_platform/services/stream_splitter.py` | P5 stream splitter (consumes raw, republishes per-plan) |
| `noble-trader-fastapi-backend/regime_platform/core/settings.py` | `NOBLE_TRADER_DISABLE_EXECUTION`, `NOBLE_TRADER_STREAM_SPLIT_ENABLED`, plan UUIDs |
