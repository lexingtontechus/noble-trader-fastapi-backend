# Noble Trader Backend Hotfix Package

## Critical Bugs Fixed

### 1. TradeRecord Missing `qty` Field
**Symptom:** `AttributeError: 'TradeRecord' object has no attribute 'qty'` on `/renko/warmup` and `/renko/tick/batch`

**Root Cause:** The deployed backend was deployed from commit before `qty` field was added to TradeRecord dataclass.

**Fix:** Ensure deployment is from commit `24fb5fb` or later which includes:
- `qty: float = 1.0` in TradeRecord (renko/trade_journal.py line 37)
- `qty=td.get("qty", 1.0)` in snapshot hydration (renko/pipeline.py line 588)
- All accesses to `record.qty` in pipeline.py (lines 958, 972, 975, 983, 993)

**Verification:**
```bash
cd /home/aloysiusteo/noble-trader-backend
git checkout 24fb5fb
python3 -c "from renko.trade_journal import TradeRecord; from renko.pattern_detector import SignalDirection, PatternType; r = TradeRecord(trade_id='TEST', symbol='SPY', direction=SignalDirection.LONG, pattern_type=PatternType.BULL_RUN); print('qty:', r.qty)"
# Should print: qty: 1.0
```

### 2. Account Status Attribute Error
**Symptom:** `AttributeError: 'TradeAccount' object has no attribute 'account_status'` on `/mode/health`

**Root Cause:** The code uses `account.status` but alpaca-trade-api 3.x may use `account.account_status` instead.

**Fix:** Apply patches to both files:

**File: regime_platform/services/operational/mode_manager.py** (line 378-384)
```diff
  account = temp_client.get_account()
+ # alpaca-trade-api 3.x may use account_status or account.status
+ status = getattr(account, 'account_status', getattr(account, 'status', 'UNKNOWN'))
  return {
      "ok": True,
-     "account_status": account.status,
+     "account_status": status,
      "equity": float(account.equity),
      "buying_power": float(account.buying_power),
  }
```

**File: regime_platform/services/operational/executor_factory.py** (line 327-336)
```diff
  account = await loop.run_in_executor(None, raw_client.get_account)
+ # alpaca-trade-api 3.x may use account_status or account.status
+ status = getattr(account, 'account_status', getattr(account, 'status', 'UNKNOWN'))
  return {
      "ok": True,
-     "account_status": account.status,
+     "account_status": status,
      "mode": self.execution_mode,
      "equity": float(account.equity),
      "buying_power": float(account.buying_power),
      "throttle": throttle.get_status(),
  }
```

**Verification:**
```bash
cd /home/aloysiusteo/noble-trader-backend
python3 -c "
from regime_platform.services.operational.executor_factory import executor_factory
import asyncio
result = asyncio.run(executor_factory.health_check())
print('Health check result:', result)
"
```

## Deployment Steps

1. **Verify commit:**
   ```bash
   cd ~/noble-trader-backend
   git log --oneline -1
   # Must be 24fb5fb or newer
   ```

2. **Pull latest:**
   ```bash
   git pull origin main
   git checkout 24fb5fb  # or newer
   ```

3. **Apply account.status patches:**
   - The patches above have already been applied locally
   - Verify with: `git diff regime_platform/services/operational/mode_manager.py`
   - Verify with: `git diff regime_platform/services/operational/executor_factory.py`

4. **Commit and push:**
   ```bash
   git add -A
   git commit -m "hotfix: trade_record_qty + account_status getattr fix"
   git push origin main
   ```

5. **Trigger Render redeployment:**
   - Render should auto-deploy on push to main
   - Check Render dashboard for deployment status

6. **Verify deployment:**
   ```bash
   # Test health endpoint
   curl -s https://noble-trader-fastapi-backend.onrender.com/mode/health \
     -H "Authorization: Bearer $(curl -s -X POST https://noble-trader-fastapi-backend.onrender.com/auth/token \
       -d 'username=trader&password=<password>' | jq -r .access_token)" \
     | jq .
   
   # Test warmup endpoint
   curl -s -X POST https://noble-trader-fastapi-backend.onrender.com/renko/warmup \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{"symbol": "SPY", "period": "1mo"}' \
     | jq .
   ```

## Local Testing

Local backend can be started for manual testing:

```bash
cd ~/noble-trader-backend
source venv/bin/activate  # or create one: python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Test locally:
```bash
# Health check
curl -s http://localhost:8000/mode/health

# Warmup
curl -s -X POST http://localhost:8000/renko/warmup \
  -H "Content-Type: application/json" \
  -d '{"symbol": "SPY", "period": "1mo"}'
```

## Post-Fix Validation Checklist

- [ ] `/mode/health` returns `{"ok": true, ...}` without AttributeError
- [ ] `/renko/warmup` completes and returns pipeline state with `brick_count > 0`
- [ ] `/renko/tick/batch` processes price batches without TradeRecord errors
- [ ] Snapshot save/restore cycle preserves `qty` field
- [ ] Trade journal correctly records `qty` in `to_dict()` output

## Notes

- The `qty` field was added in commit after `35e4eaf` (Phase 4)
- The `account.status` getattr fix is defensive coding for alpaca-trade-api version incompatibility
- Both fixes must be applied together for preflight to pass
- Render deployment may take 5-10 minutes after push