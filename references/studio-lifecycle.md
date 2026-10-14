# Studio Lifecycle: Restart, Logs, Supervisor

## Canonical Restart Sequence

**Never use `start.sh`.** Must use `on_stop.sh` → `on_start.sh`.

```bash
# 1. Kill cleanly (may need SIGKILL)
python scripts/on_stop.sh   # sends SIGTERM; if hanging, SIGKILL kills orphan

# 2. Verify all ports free
ss -tlnp | grep -E '800[1-4]' || echo "Ports clear"

# 3. Start fresh via supervisor
python scripts/on_start.sh  # Launches via `setsid nohup bash -c "..."`
```

## Service Ports

| port | service | health endpoint |
|---|---|---|
| 8001 | main.py (orchestrator + auth) | `/health` |
| 8002 | server.py (TimesFM) | `/health` |
| 8003 | sweep orchestrator | `/health` |
| 8004 | local LLM server | `/health` |
| 8000 | gateway (proxy) | *blocked by code-server permanently* |

## Log Sanitization Pattern

If orchestrator hangs on startup:
```bash
# Clear corrupted state
echo "" > logs/orchestrator.log
rm -f /tmp/orch_env.sh  # stale env file

# Restart
setsid nohup bash -c ". /tmp/svc_env.sh; cd repo; exec $PY -m main" &
```

## Supervisor Pattern

The Studio's supervisor (`supervisor.conf`) expects:
```
[program:orchestrator]
command=%(environment)POWERSHELL_HOME/../../envs/cloudspace/Scripts/python -m regime_platform.main
directory=/teamspace/studios/this_studio/noble-trader
stopwaitsecs=30
```

## Common Startup Failures

| symptom | cause | fix |
|---|---|---|
| Port 8003 in use | Previous `start.sh` left orphan | `pkill -f sweep_orchestrator` |
| Gateway 404 | `app.functions_url` unset | Refresh cron via Supabase UI |
| LLM not loading | `llm_server.py` crashed | Check `logs/llm.log` for import errors |

## Post-Stop Verification

After `on_stop.sh`:
```bash
# Confirm no Python processes from this repo
pgrep -f "noble-trader|regime_platform" && echo "STILL RUNNING" || echo "Clean"
```