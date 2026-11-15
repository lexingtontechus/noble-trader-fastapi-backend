#!/usr/bin/env python
"""run_crt_lab.py — one CLI for the CRT-0 research twin (regime_edge_crt).

Usage (repo root):
    python scripts/run_crt_lab.py                       # real corpus in data/crt/raw/
    python scripts/run_crt_lab.py --selftest            # deterministic synthetic smoke
    python scripts/run_crt_lab.py --baseline-only
    python scripts/run_crt_lab.py --stage 2 --start 2021-01-01
    python scripts/run_crt_lab.py --no-discord          # silent run (no #crt signals)

Pipeline (README_v5_CONT.md parity, ported — see
worklog/20261115_KAIROS_CRT0_BUILD.md):
    data/crt/raw/CRT_CONT_RESEARCH_<SYM>.csv   (MT4 CRT_EV_Research_Export.mq4 v1.10)
      -> A0 baseline (v3 filters, no fitting, cost burden)
      -> Stage walk-forward (max-LCB selection, CUSUM decay replay, enable gate)
      -> mint the CRT-1 engine artifact (config/crt_cont.json on REAL data;
          the selftest NEVER mints the repo artifact — synthetic data must
          not become an engine config)
      -> reports under worklog/kairos/reports/regime_edge_crt/<runid>/
      -> signals to the Discord #crt channel (fail-open; --no-discord off)

Every threshold comes from config/crt/crt_lab.yaml (self-contained config —
the toolkit's defaults verbatim). This CLI is offline research: it touches
no live surface, writes no claims, no migrations, no MT5 anything.
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import types

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from regime_edge_crt import (  # noqa: E402
    ENGINE,
    discord_notify,
)
from regime_edge_crt import baseline as crt_baseline  # noqa: E402
from regime_edge_crt import config as crt_config  # noqa: E402
from regime_edge_crt import data as crt_data  # noqa: E402
from regime_edge_crt import optimizer as crt_optimizer  # noqa: E402
from regime_edge_crt import report as crt_report  # noqa: E402
from regime_edge_crt import selftest as crt_selftest  # noqa: E402


def _fmt_event_lines(rows, cols):
    return " | ".join(str(c) for c in cols)


def _baseline_message(b):
    tab = b["buckets"]
    agg = tab[(tab["dir"] == "ALL") & (tab["session"] == "ALL")]
    lines = [f"resolved trades {b['n_resolved']:,} (setups {b['n_setups']:,}) | "
             f"data {b['data_min'][:10]} -> {b['data_max'][:10]}",
             "```", f"{'symbol':<8}{'tf':<5}{'n':>6}{'EV(R)':>9}{'t':>7}", "-" * 35]
    for _, r in agg.sort_values(["symbol", "tf"]).iterrows():
        lines.append(f"{str(r['symbol']):<8}{str(r['tf']):<5}{int(r['n']):>6}"
                     f"{float(r['ev']):>9.3f}{str(r['t']):>7}")
    lines += ["```",
              "No selection, no fitting — these numbers carry no selection bias. "
              "Positive EV here after costs = CONT has a base-rate edge worth optimizing."]
    return "\n".join(lines)


def _walkforward_message(wf):
    dep = wf["deploy_rows"]
    n_en = sum(1 for r in dep if r.get("enabled"))
    tot = sum(b["oos"]["total"] for b in wf["report_buckets"] if b.get("deployed"))
    gat = sum(b["gated_total"] for b in wf["report_buckets"] if b.get("deployed"))
    en_rows = [r for r in dep if r.get("enabled")]
    lines = [f"options deployed {len(dep)} | enabled {n_en} | folds {len(wf['folds'])} | "
             f"OOS total {tot:+.1f}R | decay-gated {gat:+.1f}R",
             "```",
             f"{'symbol':<8}{'tf':<5}{'dir':<5}{'sess':<5}{'cfg':<16}{'OOSn':>5}{'EV':>8}{'t':>6}",
             "-" * 58]
    for r in en_rows[:12]:
        cfg = (f"sb{float(r['sl_buf']):.2f} {float(r['r']):g}R h{int(r['hold'])}")
        lines.append(f"{str(r['symbol']):<8}{str(r['tf']):<5}{str(r['dir']):<5}"
                     f"{str(r['session']):<5}{cfg:<16}{int(r['oos_n']):>5}"
                     f"{float(r['base_ev']):>8.3f}{float(r['oos_t']):>6.2f}")
    if len(en_rows) > 12:
        lines.append(f"… +{len(en_rows) - 12} more enabled options")
    lines += ["```", f"train_end {wf['train_end']} — OOS = test windows the selection never saw."]
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=None, help="lab config yaml (default config/crt/crt_lab.yaml)")
    p.add_argument("--data-dir", default=None, help="override data.raw_dir")
    p.add_argument("--out-root", default=None, help="override report.out_root")
    p.add_argument("--artifact", default=None, help="override artifact.path (mint destination)")
    p.add_argument("--stage", type=int, choices=[1, 2], default=None)
    p.add_argument("--start", default=None, help="ignore setups before this date")
    p.add_argument("--models", default=None, help="e.g. CONT (default from config)")
    p.add_argument("--symbols", default=None, help="comma list filter on input CSVs")
    p.add_argument("--baseline-only", action="store_true")
    p.add_argument("--no-discord", action="store_true", help="no #crt signals this run")
    p.add_argument("--discord-config", default=None, help="discord yaml (default config/crt/crt_discord.yaml)")
    p.add_argument("--selftest", action="store_true",
                   help="deterministic synthetic corpus (seed from config) — NEVER mints the repo artifact")
    a = p.parse_args()

    cfg = crt_config.load_lab_config(a.config)
    if a.stage is not None:
        cfg["walkforward"]["stage"] = a.stage
    if a.start is not None:
        cfg["data"]["start"] = a.start
    if a.models is not None:
        cfg["data"]["models"] = a.models

    data_dir = crt_config.resolve_repo_relative(
        a.data_dir or cfg["data"]["raw_dir"], cfg["_path"])
    out_root = crt_config.resolve_repo_relative(
        a.out_root or cfg["report"]["out_root"], cfg["_path"])

    if a.selftest:
        st = cfg["selftest"]
        selftest_dir = os.path.join(REPO_ROOT, "data", "crt", "selftest")
        print(f"[selftest] generating deterministic synthetic corpus (seed {st['seed']}) "
              f"into {selftest_dir} — SYNTHETIC, not evidence; never mints the repo artifact")
        crt_selftest.generate_corpus(selftest_dir, st["symbols"], int(st["months"]),
                                     int(st["seed"]))
        data_dir = selftest_dir
        if a.artifact is None:
            cfg["artifact"]["path"] = None    # forced below into the runid dir

    import datetime as dt
    runid = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(out_root, runid)
    artifact_path = (crt_config.resolve_repo_relative(a.artifact, cfg["_path"])
                     if a.artifact else
                     (crt_config.resolve_repo_relative(cfg["artifact"]["path"], cfg["_path"])
                      if cfg["artifact"].get("path") else
                      os.path.join(out_dir, "crt_cont_SELFTEST.json")))

    csvs = sorted(glob.glob(os.path.join(
        data_dir, cfg["data"]["file_glob"] if not a.selftest else "CRT_CONT_RESEARCH_*.csv")))
    if a.symbols:
        keep = {s.strip().upper() for s in a.symbols.split(",") if s.strip()}
        csvs = [c for c in csvs if os.path.basename(c).rsplit("_", 1)[-1].removesuffix(".csv").upper() in keep]

    discord_cfg = crt_config.load_discord_config(a.discord_config)
    notifier = discord_notify.from_config(discord_cfg)
    if a.no_discord:
        notifier.enabled = False
    events_sent: list = []

    def sig(event, title, body):
        if notifier.enabled and notifier.notify(event, title, body):
            events_sent.append(event)

    try:
        if not csvs:
            raise crt_config.CRTLabError(
                f"no CRT_CONT_RESEARCH_*.csv in {data_dir} — run CRT_EV_Research_Export.mq4 "
                "v1.10 on MT4 (ExportCONT=true) and copy MQL4\\Files\\CRT_CONT_RESEARCH_<SYM>.csv "
                "into data/crt/raw/ (README_v5_CONT.md §1), or pass --selftest")

        manifest = crt_data.input_manifest(csvs)
        df, metas = crt_data.load(csvs)
        symbols = sorted(df["symbol"].unique())
        span = f"{str(df['c3_time'].min())[:10]} -> {str(df['c3_time'].max())[:10]}"

        sig("handshake", f"run {runid}",
            f"{ENGINE} | {len(csvs)} file(s): {', '.join(symbols)} | {len(df):,} rows | "
            f"{span}\ncorpus: {'SYNTHETIC selftest' if a.selftest else 'MT4 CRT_EV_Research_Export v1.10'} | "
            f"config {cfg['_status']} | stage {cfg['walkforward']['stage']}")

        # ── A0 baseline (v3 filters, no fitting) ────────────────────────────
        bparams = types.SimpleNamespace(
            model=cfg["baseline"]["model"], max_sweep=cfg["baseline"]["max_sweep"],
            min_sweep=cfg["baseline"]["min_sweep"], chop=cfg["baseline"]["chop"],
            sessions=cfg["baseline"]["sessions"], eb=cfg["baseline"]["eb"],
            sb=cfg["baseline"]["sb"], r=cfg["baseline"]["r"], hold=cfg["baseline"]["hold"],
            start=cfg["data"]["start"], out=out_dir)
        baseline_result = crt_baseline.run_baseline(bparams, df, metas)
        sig("baseline", "A0 baseline (no optimization)", _baseline_message(baseline_result))

        wf = None
        if not a.baseline_only:
            # ── walk-forward (config-driven, toolkit defaults verbatim) ────
            wf_cfg, dec_cfg = cfg["walkforward"], cfg["decay"]
            oparams = types.SimpleNamespace(
                csv=csvs, stage=wf_cfg["stage"], train_months=wf_cfg["train_months"],
                test_months=wf_cfg["test_months"], anchored=wf_cfg["anchored"],
                z=wf_cfg["z"], min_train_n=wf_cfg["min_train_n"],
                min_oos_n=wf_cfg["min_oos_n"], min_oos_ev=wf_cfg["min_oos_ev"],
                min_oos_t=wf_cfg["min_oos_t"], require_final_lcb=wf_cfg["require_final_lcb"],
                vol_low=wf_cfg["vol_low"], vol_high=wf_cfg["vol_high"],
                kappa_frac=dec_cfg["kappa_frac"], kappa_min=dec_cfg["kappa_min"],
                h_caution=dec_cfg["h_caution"], h_off=dec_cfg["h_off"],
                roll_n=dec_cfg["roll_n"], roll_off_ev=dec_cfg["roll_off_ev"],
                react_n=dec_cfg["react_n"], caution_mult=dec_cfg["caution_mult"],
                baseline=wf_cfg["baseline"], start=cfg["data"]["start"],
                models=cfg["data"]["models"], prefix=wf_cfg["prefix"], out=out_dir)
            wf = crt_optimizer.run(oparams)
            sig("walkforward", f"stage {wf_cfg['stage']} OOS verdict", _walkforward_message(wf))

            # ── mint the CRT-1 engine artifact ─────────────────────────────
            artifact = crt_report.build_artifact(cfg, wf, manifest, runid)
            crt_report.write_json(artifact_path, artifact)
            n_en = sum(1 for r in artifact["rows"] if r.get("enabled"))
            print(f"minted {artifact_path}  ({n_en}/{len(artifact['rows'])} options enabled)")
            sig("mint", os.path.basename(artifact_path),
                f"{len(artifact['rows'])} option rows | {n_en} enabled | "
                f"train_end {artifact['meta']['train_end']}\n"
                f"inputs: {', '.join(i['path'] for i in manifest)} (sha256 stamped)\n"
                + ("SELFTEST-SYNTHETIC — will be overwritten by the first real-data run"
                   if a.selftest else "CRT-1 (crt/ engine) reads this artifact"))

        # ── the run record + summary ───────────────────────────────────────
        crt_report.write_json(os.path.join(out_dir, "crt_lab.json"),
                              crt_report.build_run_record(cfg, runid, manifest,
                                                           baseline_result, wf, events_sent))
        with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as fh:
            fh.write(crt_report.build_summary_md(runid, cfg, baseline_result, wf, artifact_path))
        print(f"\nrun {runid} complete: {out_dir}{os.sep}"
              f" | #crt signals sent: {len(events_sent)} ({', '.join(events_sent) or 'none'})")
        return 0
    except crt_config.CRTLabError as exc:
        print(f"CRT-0 lab error: {exc}", file=sys.stderr)
        sig("error", f"run {runid} failed", f"```\n{exc}\n```")
        return 2
    finally:
        notifier.close()


if __name__ == "__main__":
    sys.exit(main())
