#!/usr/bin/env python3
"""watchdog_liveness.py — external liveness watchdog for the signal engine.

One-shot and cron-driven (it does not loop). Reuses the exact heartbeat set and
SQL from ``pulse_check_r2.py``'s liveness gate so there is a single source of
truth for "is the engine producing".

WHY DATA-BASED AND NOT AN HTTP PING
───────────────────────────────────
On 2026-10-16 the platform went dark for ~21 hours. An HTTP health check on the
gateway would have returned 200 for as long as the process happened to be alive
— it cannot see a *silent* stall, which is exactly what happened. This queries
``max(timestamp)`` for every writer and compares it to ``now()``: the only check
that catches an engine which is up but not producing.

DEPENDENCIES
────────────
Standard library only — deliberately. It must run on a bare machine with no
``pip install`` step, so it can be dropped anywhere (a cron box, a CI runner, an
instabox machine driven by ``instabox exec``).

ENVIRONMENT
───────────
    SUPABASE_ACCESS_TOKEN   management-API token (required)
    SUPABASE_URL            project URL; the ref is parsed out of it (required)
    DISCORD_WEBHOOK_STATUS  #system-status webhook (optional; prints if unset)

EXIT CODES
──────────
    0  engine healthy
    2  engine DOWN (a fast heartbeat is stale)
    3  the check itself failed (could not reach Supabase)
    4  configuration missing

Usage
─────
    ./watchdog_liveness.py                       # one check, alert on change
    ./watchdog_liveness.py --dry-run             # never post, just report
    ./watchdog_liveness.py --realert-min 30      # re-page every 30m while down
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── Single source of truth, mirrored from scripts/pulse_check_r2.py ─────────
# (label, json key, stale-after minutes, expected cadence)
HEARTBEATS: tuple[tuple[str, str, float, str], ...] = (
    ("sweep", "sweep_age_min", 30, "light sweep every 15m"),
    ("signal_sim", "signal_age_min", 30, "one row per emitted signal"),
    ("pbm_stamp", "pbm_stamp_age_min", 30, "stamped with the sim row"),
    ("roadmap_heartbeat", "roadmap_age_min", 150, "hourly kairos-daily"),
    ("cell_posterior", "cell_posterior_age_min", 400, "resolver + rebuild"),
    ("calib_state", "calib_state_age_min", 400, "4h correction slots"),
    ("macro_regime", "macro_age_min", 1800, "daily 06:00/06:10 UTC"),
    ("bracket_reachability", "reachability_age_min", 11520, "nightly + weekly Sun"),
)

# The engine is not producing if any of these is stale.
FAST_HEARTBEATS = frozenset({"sweep", "signal_sim", "pbm_stamp"})

LIVENESS_SQL = """
select
  round(extract(epoch from now() - (select max(sweep_timestamp) from nt_sweep_result))/60.0, 1)
    as sweep_age_min,
  round(extract(epoch from now() - (select max(ts) from nt_signal_sim))/60.0, 1)
    as signal_age_min,
  round(extract(epoch from now() - (select max(ts) from nt_pbm_shadow))/60.0, 1)
    as pbm_stamp_age_min,
  round(extract(epoch from now() - (select max(updated_at) from nt_roadmap))/60.0, 1)
    as roadmap_age_min,
  round(extract(epoch from now() - (select max(updated_at) from nt_cell_posterior))/60.0, 1)
    as cell_posterior_age_min,
  round(extract(epoch from now() - (select max(computed_at) from nt_symbol_calibration_state))/60.0, 1)
    as calib_state_age_min,
  round(extract(epoch from now() - (select max(computed_at) from nt_macro_regime))/60.0, 1)
    as macro_age_min,
  round(extract(epoch from now() - (select max(computed_at) from nt_bracket_reachability))/60.0, 1)
    as reachability_age_min;
"""

DEFAULT_STATE = Path(os.environ.get("WATCHDOG_STATE") or "/data/watchdog_state.json")


def _load_dotenv(path: Path) -> None:
    """Fill os.environ from a .env file without overwriting what is already set.

    instabox's Vault injects real env vars at boot; a local .env is only for
    running this by hand. Existing values always win.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _post_json(url: str, payload: dict, headers: dict[str, str]) -> object:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else None


def fetch_ages(token: str, ref: str) -> dict[str, float | None]:
    """Return {json key: age in minutes} from the management API."""
    rows = _post_json(
        f"https://api.supabase.com/v1/projects/{ref}/database/query",
        {"query": LIVENESS_SQL},
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"unexpected liveness response: {rows!r}")
    row = rows[0]
    ages: dict[str, float | None] = {}
    for _label, key, _limit, _note in HEARTBEATS:
        raw = row.get(key)
        try:
            # the management API returns round(numeric, n) as a JSON STRING
            ages[key] = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            ages[key] = None
    return ages


def evaluate(ages: dict[str, float | None]) -> tuple[list[str], list[str], list[str]]:
    """Classify heartbeats → (dead, stale, down-fast-heartbeats)."""
    dead: list[str] = []
    stale: list[str] = []
    for label, key, limit, _note in HEARTBEATS:
        age = ages.get(key)
        if age is None or age > limit * 2:
            dead.append(label)
        elif age > limit:
            stale.append(label)
    fast_down = [name for name in dead + stale if name in FAST_HEARTBEATS]
    return dead, stale, fast_down


def fmt_age(minutes: float | None) -> str:
    if minutes is None:
        return "never"
    if minutes < 90:
        return f"{minutes:.0f}m"
    if minutes < 60 * 48:
        return f"{minutes / 60:.1f}h"
    return f"{minutes / 1440:.1f}d"


def render(ages: dict[str, float | None], dead: list[str], stale: list[str]) -> str:
    lines = []
    for label, key, limit, note in HEARTBEATS:
        age = ages.get(key)
        if label in dead:
            state = "DEAD"
        elif label in stale:
            state = "STALE"
        else:
            state = "ok"
        mark = " " if state == "ok" else "!"
        lines.append(f"{mark} {label:22s} {fmt_age(age):>7s}  {state:<5s} (stale > {fmt_age(limit)}; {note})")
    return "\n".join(lines)


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"watchdog: could not write state {path}: {exc}", file=sys.stderr)


def post_discord(webhook: str, content: str) -> bool:
    try:
        _post_json(webhook, {"content": content[:1990]}, {"Content-Type": "application/json"})
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        print(f"watchdog: discord post failed: {exc}", file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="External liveness watchdog (one-shot).")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--realert-min", type=int, default=30, help="while DOWN, re-page at most this often (default 30)")
    ap.add_argument("--dry-run", action="store_true", help="never post to Discord")
    ap.add_argument("--env-file", type=Path, default=Path(".env.local"))
    args = ap.parse_args()

    _load_dotenv(args.env_file)

    token = os.environ.get("SUPABASE_ACCESS_TOKEN", "").strip()
    # The project ref is parsed out of SUPABASE_URL — the variable .env.local
    # actually carries. There is deliberately no separate ref variable.
    host = os.environ.get("SUPABASE_URL", "").strip().split("//")[-1].split("/")[0]
    ref = host[: -len(".supabase.co")] if host.endswith(".supabase.co") else ""
    webhook = os.environ.get("DISCORD_WEBHOOK_STATUS", "").strip()

    if not token or not ref:
        print(
            "watchdog: need SUPABASE_ACCESS_TOKEN and SUPABASE_URL (e.g. https://<ref>.supabase.co)",
            file=sys.stderr,
        )
        return 4

    try:
        ages = fetch_ages(token, ref)
    except Exception as exc:  # noqa: BLE001 — surface anything, never crash silently
        print(f"watchdog: CHECK FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        # A failed check is not the same as a dead engine, but it is not "ok"
        # either — say so loudly and let the exit code carry it.
        return 3

    dead, stale, fast_down = evaluate(ages)
    report = render(ages, dead, stale)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    now_epoch = time.time()

    prev = load_state(args.state)
    was_down = prev.get("state") == "down"
    is_down = bool(fast_down)

    print(f"=== kairos watchdog {now_iso} ===")
    print(report)

    if not is_down:
        print("\nall fast heartbeats fresh — engine producing")
        if was_down:
            downtime = ""
            if prev.get("since"):
                try:
                    started = datetime.fromisoformat(prev["since"])
                    mins = (now - started).total_seconds() / 60.0
                    downtime = f" after ~{fmt_age(mins)} down"
                except ValueError:
                    pass
            msg = f":white_check_mark: **Kairos recovered**{downtime} — sweep/signal/PBM producing again."
            print(msg)
            if webhook and not args.dry_run:
                post_discord(webhook, msg)
        save_state(args.state, {"state": "up", "checked": now_iso})
        return 0

    # ── DOWN ────────────────────────────────────────────────────────────────
    print(f"\n*** SIGNAL ENGINE DOWN — {', '.join(fast_down)} not producing ***")
    since = prev.get("since") if was_down else now_iso
    last_alert = float(prev.get("last_alert_epoch") or 0)
    due = (not was_down) or (now_epoch - last_alert >= args.realert_min * 60)

    if due:
        detail = "\n".join(
            f"! {label:20s} {fmt_age(ages.get(key)):>7s}  {('DEAD' if label in dead else 'STALE')}"
            for label, key, _limit, _note in HEARTBEATS
            if label in fast_down
        )
        msg = (
            f":rotating_light: **KAIROS SIGNAL ENGINE DOWN**\n"
            f"`{', '.join(fast_down)}` not producing.\n```\n{detail}\n```\n"
            f"First seen: {since}"
        )
        print(msg)
        # Record the alert time whenever we DECIDE to alert, not only when the
        # webhook actually posts — otherwise dry-run (and a missing webhook)
        # would re-alert on every single run.
        last_alert = now_epoch
        if webhook and not args.dry_run:
            post_discord(webhook, msg)
    else:
        mins = (now_epoch - last_alert) / 60.0 if last_alert else 0
        print(f"(already alerted {fmt_age(mins)} ago; re-alert at {args.realert_min}m)")

    save_state(
        args.state,
        {
            "state": "down",
            "since": since,
            "checked": now_iso,
            "last_alert_epoch": last_alert,
            "down": fast_down,
            "dead": dead,
            "stale": stale,
        },
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
