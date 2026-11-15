# 2026-11-15 — #crt Webhook Moves to `DISCORD_WEBHOOK_CRT` (env-first)

## Context

Operator instruction: "update the crt discord webhook to use -
DISCORD_WEBHOOK_CRT". CRT-0 (commit b49e2bc) had committed the webhook URL
into `config/crt/crt_discord.yaml` by explicit instruction, with a documented
rotation note. This change moves the secret OUT of the repo and into the
environment variable `DISCORD_WEBHOOK_CRT`.

## The change (3 files, ~30 lines)

- `regime_edge_crt/config.py::load_discord_config` — the webhook URL now
  resolves **env-first**: `DISCORD_WEBHOOK_CRT` (stripped) overrides the
  yaml's `webhook_url`; the yaml value remains a documented fallback for
  deployments that prefer file config; neither set → the sender stays
  disabled (fail-open, `_status`/`_webhook_source` stamps record which source
  was applied: `env:DISCORD_WEBHOOK_CRT` | `yaml` | `unset`).
- `config/crt/crt_discord.yaml` — `webhook_url: ""` (the secret left the
  repo); header documents the env var and the rotation procedure.
- `regime_edge_crt/discord_notify.py` — docstring updated to the new
  contract; the event envelope label is now configurable (`label`, default
  `"CRT-0"`) so the CRT-1 engine can signal as `CRT-1` through the SAME
  sender without a second notifier.

Behavior notes:

- `--no-discord` / `enabled: false` semantics unchanged; the fail-open,
  bounded, throttled, chunked, masked contract is unchanged (verified
  live-delivery behavior in CRT-0; masking re-verified here).
- Engines/labs that never set the env var simply skip signals — nothing
  fails.

## Verification

- Resolution chain (in the CRT-1 suite, group K, and standalone): unset +
  blank yaml → disabled + source `unset`; env var set → precedence over yaml
  + source `env:DISCORD_WEBHOOK_CRT`; yaml-only fallback works; env beats
  yaml; the committed yaml contains no webhook secret (old URL/token absent).
- Envelope: `event_message(..., label="CRT-1")` renders the CRT-1 header;
  `CRTDiscord.label` drives `notify()`.
- **Live delivery attempt**: the PRIOR webhook URL (from the CRT-0 session)
  now returns `404 Unknown Webhook` — the operator evidently rotated it,
  which is precisely the scenario the env var exists for. Fail-open held
  (logged + skipped, nothing raised). **Operator action required**: set
  `DISCORD_WEBHOOK_CRT` to the current webhook on the deployment box
  (`export DISCORD_WEBHOOK_CRT="https://discord.com/api/webhooks/<id>/<token>"`).
- Regressions: `tests/test_router_edge.py` + `tests/test_strategy_registry.py`
  46/46; the CRT-0 selftest pipeline end-to-end (RC 0, mint, reports) with
  signals correctly skipped (webhook unset).

## Rotation history note (honesty)

The original webhook URL remains in git history (b49e2bc and this commit's
parent). Removing it from HEAD does not purge history. Since the old URL is
already dead (404), no action is required; if this repo ever widens its
audience, rotate again — the env var makes that a zero-code operation.
