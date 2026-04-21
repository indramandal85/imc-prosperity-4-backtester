#!/usr/bin/env python3
"""
IMC Prosperity 4 — Hyperparameter Tuner
========================================
Runs 275814_optimized.py (or any algorithm) with patched PARAMS dicts
through the Rust backtester, collects metrics.json, and reports a ranked
results table grouped by parameter.

Usage:
    python3 hyperparam_tuner.py [--algo PATH] [--dataset DATASET]
                                [--mode grid|random] [--n-random N]
                                [--out CSV_PATH]

Examples:
    # Full grid search on default param space:
    python3 hyperparam_tuner.py

    # Random search (200 samples) with a different algo file:
    python3 hyperparam_tuner.py --algo ../275814/275814.py --mode random --n-random 200

    # Only run the ASH_TAKE_EDGE sweep, no other combos:
    python3 hyperparam_tuner.py --params ASH_TAKE_EDGE


cd prosperity_rust_backtester-main

# 1-D sweep (one param at a time, ~70 runs, ~60s) — best for diagnosis
python3 hyperparam_tuner.py --mode 1d

# Random search (N combos, ~80s for 80 runs) — best for multi-param combos
python3 hyperparam_tuner.py --mode random --n-random 100

# Target only specific params
python3 hyperparam_tuner.py --mode 1d --params ASH_TAKE_EDGE ASH_EMA_WEIGHT

# Different algo file
python3 hyperparam_tuner.py --algo ../round2_algo.py --mode 1d
"""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import itertools
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ────────────────────────────────────────────────────────────────────────────────
# Configuration
# ────────────────────────────────────────────────────────────────────────────────

BACKTESTER_BIN = Path.home() / "Library/Caches/rust_backtester/target/release/rust_backtester"
BACKTESTER_DIR = Path(__file__).parent          # must run from there (datasets/ relative)
DEFAULT_DATASET = "round2"
DEFAULT_ALGO    = str(Path(__file__).parent.parent / "275814" / "275814_optimized.py")

ASH = "ASH_COATED_OSMIUM"
IPR = "INTARIAN_PEPPER_ROOT"

# ── Hyperparameter search space ─────────────────────────────────────────────────
# Format:  "PARAM_KEY": [value1, value2, ...]
# Only params listed here will be swept. Everything else stays at algo defaults.
PARAM_SPACE: Dict[str, List[Any]] = {
    # --- Core take-edge ---
    "ASH_TAKE_EDGE":          [0.0, 0.5, 1.0, 1.5, 2.0],

    # --- Spike passive layers (Enhancement A) ---
    "ASH_SPIKE_ASK_EDGE":     [6, 8, 10, 12, 14],
    "ASH_SPIKE_BID_EDGE":     [4, 6, 8, 10],
    "ASH_SPIKE_SIZE":         [5, 8, 10, 12, 15],

    # --- Touch geometry ---
    "ASH_TOUCH_SIZE_BUY":     [12, 15, 20, 25],
    "ASH_TOUCH_SIZE_SELL":    [10, 12, 15, 20],
    "ASH_TOUCH_CAP_FROM_FAIR_BID": [1.0, 1.5, 2.0, 2.5],
    "ASH_TOUCH_CAP_FROM_FAIR_ASK": [2.0, 2.5, 3.0, 3.5],

    # --- Anchor geometry ---
    "ASH_ANCHOR_BID_EDGE":    [4, 5, 6, 7],
    "ASH_ANCHOR_ASK_EDGE":    [5, 6, 7, 8, 9],

    # --- Deep bid ---
    "ASH_DEEP_BID_EDGE":      [5, 6, 7, 8, 9],
    "ASH_DEEP_BID_SPREAD_MIN": [12, 14, 16, 18],
    "ASH_DEEP_BID_SIZE":      [12, 15, 20, 25],

    # --- EMA ---
    "ASH_EMA_WEIGHT":         [0.3, 0.4, 0.5, 0.6],

    # --- Clear tolerance ---
    "ASH_CLEAR_TOLERANCE":    [1.0, 1.5, 2.0, 2.5],

    # --- Regime bias (Enhancement B — set BIAS_MAX=0 to disable) ---
    "ASH_REGIME_BIAS_MAX":    [0, 5, 10, 20],

    # --- Asymmetric take edges (Enhancement C — set THRESHOLD=999 to disable) ---
    "ASH_REGIME_THRESHOLD":   [3.0, 5.0, 10.0, 999.0],
}

# ── Groups for 1-D sensitivity reports ────────────────────────────────────────
PARAM_GROUPS = {
    "core":    ["ASH_TAKE_EDGE", "ASH_EMA_WEIGHT", "ASH_CLEAR_TOLERANCE"],
    "spike":   ["ASH_SPIKE_ASK_EDGE", "ASH_SPIKE_BID_EDGE", "ASH_SPIKE_SIZE"],
    "touch":   ["ASH_TOUCH_SIZE_BUY", "ASH_TOUCH_SIZE_SELL",
                "ASH_TOUCH_CAP_FROM_FAIR_BID", "ASH_TOUCH_CAP_FROM_FAIR_ASK"],
    "anchor":  ["ASH_ANCHOR_BID_EDGE", "ASH_ANCHOR_ASK_EDGE"],
    "deep":    ["ASH_DEEP_BID_EDGE", "ASH_DEEP_BID_SPREAD_MIN", "ASH_DEEP_BID_SIZE"],
    "regime":  ["ASH_REGIME_BIAS_MAX", "ASH_REGIME_THRESHOLD"],
}

# ────────────────────────────────────────────────────────────────────────────────
# Algorithm patcher
# ────────────────────────────────────────────────────────────────────────────────

def patch_params(src: str, overrides: Dict[str, Any]) -> str:
    """
    Patch a PARAMS dict literal inside source code.

    Approach: scan line by line. Inside the PARAMS block, detect lines of the
    form  (whitespace) "KEY": value, (optional comment)  and replace value.
    Skips comment lines (starting with #). Works for any comment placement.
    """
    if not overrides:
        return src

    lines = src.split("\n")
    out = []
    in_params = False
    brace_depth = 0

    for line in lines:
        if not in_params:
            # Detect start of PARAMS dict assignment (handles type annotation too)
            if re.match(r'\s*PARAMS\s*(?::[^=]*)?\s*=\s*\{', line):
                in_params = True
                brace_depth = line.count("{") - line.count("}")
                out.append(line)
                continue
        else:
            brace_depth += line.count("{") - line.count("}")
            if brace_depth <= 0:
                in_params = False
                out.append(line)
                continue

            # Skip pure comment lines
            stripped = line.strip()
            if stripped.startswith("#"):
                out.append(line)
                continue

            # Try to match an active key-value line:
            #   (spaces) "KEY": VALUE, (optional # comment)
            m = re.match(
                r'^(\s*"([^"]+)"\s*:\s*)([^,#\n]+?)\s*(,\s*(?:#.*)?)$',
                line
            )
            if m and m.group(2) in overrides:
                key = m.group(2)
                val = overrides[key]
                # Format value
                if isinstance(val, bool):
                    val_str = "True" if val else "False"
                elif isinstance(val, float):
                    # Keep as float literal (e.g. 1.0, 0.5)
                    val_str = str(val)
                else:
                    val_str = str(val)
                line = f"{m.group(1)}{val_str}{m.group(4)}"

        out.append(line)
    return "\n".join(out)


def write_temp_algo(src_path: str, overrides: Dict[str, Any]) -> str:
    """Write a patched version to a temp file; return the path."""
    with open(src_path) as f:
        src = f.read()
    patched = patch_params(src, overrides)
    fd, tmp_path = tempfile.mkstemp(suffix=".py", prefix="algo_")
    with os.fdopen(fd, "w") as f:
        f.write(patched)
    return tmp_path

# ────────────────────────────────────────────────────────────────────────────────
# Backtester runner
# ────────────────────────────────────────────────────────────────────────────────

def run_backtester(algo_path: str, dataset: str = DEFAULT_DATASET,
                   days: Optional[List[int]] = None,
                   extra_args: Optional[List[str]] = None) -> Dict:
    """
    Run the Rust backtester for all days in the dataset.
    Returns a dict with:
        {
          "total_pnl": float,
          "ash_pnl": float,
          "ipr_pnl": float,
          "ash_by_day": {-1: ..., 0: ..., 1: ...},
          "total_by_day": {...},
          "own_trades": int,
          "days_run": int,
        }
    """
    cmd = [
        str(BACKTESTER_BIN),
        "--trader", algo_path,
        "--dataset", dataset,
        "--products", "summary",
    ]
    if extra_args:
        cmd.extend(extra_args)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=120,
            cwd=str(BACKTESTER_DIR),
        )
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    except Exception as e:
        return {"error": str(e)}

    stdout = result.stdout
    if result.returncode != 0:
        return {"error": result.stderr[:200]}

    # Parse the text output — lines look like:
    #   D-1   -1   10000   783   99132.00   runs/...
    #   TOTAL   -   30000  2305  296305.00  -
    #   INTARIAN_PEPPER_ROOT   79322.00   79423.00   79364.00  238109.00
    #   ASH_COATED_OSMIUM      19810.00   17988.00   20398.00   58196.00

    metrics: Dict = {
        "total_pnl": 0.0,
        "ash_pnl": 0.0,
        "ipr_pnl": 0.0,
        "ash_by_day": {},
        "ipr_by_day": {},
        "total_by_day": {},
        "own_trades": 0,
        "days_run": 0,
    }

    day_map = {"D-1": -1, "D=0": 0, "D+1": 1}

    for line in stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        # TOTAL row
        if parts[0] == "TOTAL" and len(parts) >= 5:
            try:
                metrics["total_pnl"] = float(parts[4])
                metrics["own_trades"] = int(parts[3])
            except (ValueError, IndexError):
                pass
        # Per-day row: D-1  -1  10000  783  99132.00  ...
        elif parts[0] in day_map and len(parts) >= 5:
            try:
                day = day_map[parts[0]]
                metrics["total_by_day"][day] = float(parts[4])
                metrics["days_run"] += 1
            except (ValueError, IndexError):
                pass
        # Product row:
        elif parts[0] == ASH and len(parts) >= 5:
            try:
                metrics["ash_pnl"] = float(parts[-1])
                day_vals = [float(v) for v in parts[1:-1] if v.replace(".", "").replace("-", "").isdigit()]
                for i, d in enumerate([-1, 0, 1]):
                    if i < len(day_vals):
                        metrics["ash_by_day"][d] = day_vals[i]
            except (ValueError, IndexError):
                pass
        elif parts[0] == IPR and len(parts) >= 5:
            try:
                metrics["ipr_pnl"] = float(parts[-1])
                day_vals = [float(v) for v in parts[1:-1] if v.replace(".", "").replace("-", "").isdigit()]
                for i, d in enumerate([-1, 0, 1]):
                    if i < len(day_vals):
                        metrics["ipr_by_day"][d] = day_vals[i]
            except (ValueError, IndexError):
                pass

    return metrics

# ────────────────────────────────────────────────────────────────────────────────
# Search strategies
# ────────────────────────────────────────────────────────────────────────────────

def grid_configs(param_space: Dict[str, List[Any]],
                 selected_params: Optional[List[str]] = None) -> Iterable[Dict[str, Any]]:
    """Yield all combinations in the grid (or subset if selected_params given)."""
    if selected_params:
        space = {k: param_space[k] for k in selected_params if k in param_space}
    else:
        space = param_space
    keys = list(space.keys())
    for combo in itertools.product(*[space[k] for k in keys]):
        yield dict(zip(keys, combo))


def random_configs(param_space: Dict[str, List[Any]], n: int,
                   selected_params: Optional[List[str]] = None) -> Iterable[Dict[str, Any]]:
    """Yield n random samples from the search space."""
    if selected_params:
        space = {k: param_space[k] for k in selected_params if k in param_space}
    else:
        space = param_space
    keys = list(space.keys())
    seen = set()
    attempts = 0
    while len(seen) < n and attempts < n * 10:
        attempts += 1
        cfg = {k: random.choice(v) for k, v in space.items() if k in keys}
        sig = tuple(sorted(cfg.items()))
        if sig not in seen:
            seen.add(sig)
            yield cfg


def one_at_a_time_configs(param_space: Dict[str, List[Any]],
                           base_overrides: Dict[str, Any],
                           selected_params: Optional[List[str]] = None) -> Iterable[Tuple[str, Any, Dict]]:
    """Yield (param_name, param_value, full_overrides) varying one param at a time."""
    keys = selected_params or list(param_space.keys())
    for key in keys:
        for val in param_space.get(key, []):
            overrides = {**base_overrides, key: val}
            yield key, val, overrides

# ────────────────────────────────────────────────────────────────────────────────
# Results collection and reporting
# ────────────────────────────────────────────────────────────────────────────────

def config_hash(cfg: Dict) -> str:
    sig = json.dumps(cfg, sort_keys=True)
    return hashlib.md5(sig.encode()).hexdigest()[:8]


class ResultStore:
    def __init__(self):
        self.records: List[Dict] = []

    def add(self, overrides: Dict, metrics: Dict, label: str = ""):
        rec = {
            "label": label,
            **{f"P_{k}": v for k, v in overrides.items()},
            "total_pnl":    metrics.get("total_pnl", 0),
            "ash_total":    metrics.get("ash_pnl", 0),
            "ipr_total":    metrics.get("ipr_pnl", 0),
            "ash_d-1":      metrics.get("ash_by_day", {}).get(-1, 0),
            "ash_d0":       metrics.get("ash_by_day", {}).get(0, 0),
            "ash_d+1":      metrics.get("ash_by_day", {}).get(1, 0),
            "own_trades":   metrics.get("own_trades", 0),
            "error":        metrics.get("error", ""),
        }
        self.records.append(rec)

    def sorted_by(self, key: str = "ash_total") -> List[Dict]:
        return sorted(self.records, key=lambda r: r.get(key, 0), reverse=True)

    def best(self, key: str = "ash_total") -> Optional[Dict]:
        good = [r for r in self.records if not r.get("error")]
        if not good:
            return None
        return max(good, key=lambda r: r.get(key, 0))

    def save_csv(self, path: str):
        if not self.records:
            return
        # Union of all keys across all records (handles 1-D where each rec has 1 param)
        all_keys: list = []
        seen_keys: set = set()
        for rec in self.records:
            for k in rec:
                if k not in seen_keys:
                    seen_keys.add(k)
                    all_keys.append(k)
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore",
                               restval="")
            w.writeheader()
            # Fill missing keys with empty string for rows that don't have them
            rows = []
            for rec in self.sorted_by():
                row = {k: rec.get(k, "") for k in all_keys}
                rows.append(row)
            w.writerows(rows)

    def print_top(self, n: int = 20, sort_key: str = "ash_total",
                  base_ash: Optional[float] = None):
        good = [r for r in self.records if not r.get("error")]
        top = sorted(good, key=lambda r: r.get(sort_key, 0), reverse=True)[:n]
        if not top:
            print("  (no valid results)")
            return
        param_keys = [k for k in top[0] if k.startswith("P_")]
        print()
        # Header
        hdr = f"{'#':>3}  {'ASH_TOT':>9}  {'Δbase':>7}  {'D-1':>7}  {'D0':>7}  {'D+1':>7}  {'TOTAL':>9}"
        for pk in param_keys:
            hdr += f"  {pk[2:]:>20}"
        print(hdr)
        print("-" * len(hdr))
        for rank, r in enumerate(top, 1):
            delta = f"{r['ash_total'] - base_ash:+.0f}" if base_ash is not None else "  n/a"
            row = (f"{rank:>3}  {r['ash_total']:>9.0f}  {delta:>7}"
                   f"  {r['ash_d-1']:>7.0f}  {r['ash_d0']:>7.0f}  {r['ash_d+1']:>7.0f}"
                   f"  {r['total_pnl']:>9.0f}")
            for pk in param_keys:
                row += f"  {str(r.get(pk, '')):>20}"
            print(row)

    def sensitivity_report(self, param_space: Dict, base_ash: float):
        """For each param, show average ASH PnL per value."""
        good = [r for r in self.records if not r.get("error")]
        if not good:
            return
        print()
        print("═" * 70)
        print("  SENSITIVITY REPORT  (avg ASH PnL per param value)")
        print("═" * 70)
        for pk in [k for k in good[0] if k.startswith("P_")]:
            param = pk[2:]
            by_val: Dict = defaultdict(list)
            for r in good:
                by_val[r.get(pk)].append(r["ash_total"])
            print(f"\n  {param}")
            print(f"  {'Value':>10}  {'Avg ASH PnL':>12}  {'Δbase':>8}  {'Count':>6}")
            print(f"  {'-'*40}")
            sorted_vals = sorted(by_val.items(),
                                 key=lambda x: -(sum(x[1]) / len(x[1])))
            for val, pnls in sorted_vals:
                avg = sum(pnls) / len(pnls)
                delta = avg - base_ash
                marker = " ◀ BEST" if val == sorted_vals[0][0] else ""
                print(f"  {str(val):>10}  {avg:>12.0f}  {delta:>+8.0f}  {len(pnls):>6}{marker}")

# ────────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="IMC Hyperparameter Tuner")
    p.add_argument("--algo",     default=DEFAULT_ALGO,    help="Path to algorithm .py file")
    p.add_argument("--dataset",  default=DEFAULT_DATASET, help="Dataset name (e.g. round2)")
    p.add_argument("--mode",     default="1d",
                   choices=["grid", "random", "1d"],
                   help="Search mode: 1d=one-at-a-time, grid=full grid, random=random sample")
    p.add_argument("--n-random", type=int, default=200,   help="Samples for random mode")
    p.add_argument("--params",   nargs="+", default=None, help="Restrict to these params only")
    p.add_argument("--out",      default="tuner_results.csv", help="CSV output path")
    p.add_argument("--sort",     default="ash_total",
                   choices=["ash_total", "total_pnl"],   help="Sort key for results")
    p.add_argument("--top",      type=int, default=25,   help="Rows to show in top table")
    p.add_argument("--seed",     type=int, default=42,   help="Random seed")
    return p.parse_args()


def main():
    args = parse_args()
    random.seed(args.seed)

    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║    IMC Prosperity 4 — Hyperparameter Tuner v1.0         ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print(f"  algo:    {args.algo}")
    print(f"  dataset: {args.dataset}")
    print(f"  mode:    {args.mode}")
    print(f"  params:  {args.params or 'all'}")
    print()

    if not BACKTESTER_BIN.exists():
        print(f"ERROR: Rust backtester not found at {BACKTESTER_BIN}")
        sys.exit(1)
    if not os.path.exists(args.algo):
        print(f"ERROR: Algorithm file not found: {args.algo}")
        sys.exit(1)

    store = ResultStore()

    # ── Baseline run (no overrides) ────────────────────────────────────────────
    print("→ Running BASELINE (no param overrides)...")
    base_m = run_backtester(args.algo, args.dataset)
    if "error" in base_m:
        print(f"  BASELINE ERROR: {base_m['error']}")
        sys.exit(1)
    base_ash   = base_m["ash_pnl"]
    base_total = base_m["total_pnl"]
    store.add({}, base_m, "BASELINE")
    print(f"  BASELINE:  ASH={base_ash:.0f}  IPR={base_m['ipr_pnl']:.0f}  TOTAL={base_total:.0f}")
    print(f"             ASH by day: {base_m['ash_by_day']}")
    print()

    # ── Generate config list ───────────────────────────────────────────────────
    if args.mode == "1d":
        # One-at-a-time: fix everything else at baseline, sweep one param
        configs_iter = one_at_a_time_configs(PARAM_SPACE, {}, args.params)
        configs = [(param, val, cfg) for param, val, cfg in configs_iter]
        total_runs = len(configs)
        print(f"→ Mode: 1-D sweep ({total_runs} configs, one param at a time)")
    elif args.mode == "grid":
        raw = list(grid_configs(PARAM_SPACE, args.params))
        configs = [("grid", None, c) for c in raw]
        total_runs = len(configs)
        print(f"→ Mode: Grid search ({total_runs} configs)")
    else:
        raw = list(random_configs(PARAM_SPACE, args.n_random, args.params))
        configs = [("random", None, c) for c in raw]
        total_runs = len(configs)
        print(f"→ Mode: Random search ({total_runs} configs)")

    print(f"  Starting runs... (Ctrl+C to stop early and see partial results)\n")

    t_start = time.time()
    completed = 0

    try:
        for i, config_entry in enumerate(configs):
            if args.mode == "1d":
                param_name, param_val, overrides = config_entry
                label = f"{param_name}={param_val}"
            else:
                _, _, overrides = config_entry
                label = config_hash(overrides)

            tmp_path = write_temp_algo(args.algo, overrides)
            try:
                m = run_backtester(tmp_path, args.dataset)
            finally:
                os.unlink(tmp_path)

            store.add(overrides, m, label)
            completed += 1

            ash = m.get("ash_pnl", 0)
            delta = ash - base_ash
            elapsed = time.time() - t_start
            rate = completed / elapsed if elapsed > 0 else 1
            remaining = (total_runs - completed) / rate if rate > 0 else 0

            status = m.get("error", "")
            if not status:
                print(f"  [{completed:>4}/{total_runs}]  {label:<35}  ASH={ash:>8.0f}  Δ={delta:>+8.0f}"
                      f"  eta={remaining:.0f}s")
            else:
                print(f"  [{completed:>4}/{total_runs}]  {label:<35}  ERROR: {status}")

    except KeyboardInterrupt:
        print("\n  (interrupted by user)")

    # ── Results ────────────────────────────────────────────────────────────────
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print(f"║   RESULTS  ({completed} runs in {time.time()-t_start:.0f}s)                        ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print(f"\n  BASELINE: ASH={base_ash:.0f}  TOTAL={base_total:.0f}")

    best = store.best(args.sort)
    if best:
        best_ash = best["ash_total"]
        print(f"\n  ★ BEST CONFIG:  ASH={best_ash:.0f}  Δbase={best_ash-base_ash:+.0f}")
        print(f"     D-1={best['ash_d-1']:.0f}  D0={best['ash_d0']:.0f}  D+1={best['ash_d+1']:.0f}")
        param_vals = {k[2:]: v for k, v in best.items() if k.startswith("P_")}
        if param_vals:
            print(f"     Params: {param_vals}")

    print(f"\n  TOP {args.top} CONFIGS (sorted by {args.sort}):")
    store.print_top(args.top, args.sort, base_ash)

    if args.mode == "1d":
        store.sensitivity_report(PARAM_SPACE, base_ash)

    # ── Save CSV ───────────────────────────────────────────────────────────────
    out_path = os.path.join(os.path.dirname(args.algo), args.out)
    store.save_csv(out_path)
    print(f"\n  Full results saved to: {out_path}")

    # ── Winning config snippet ─────────────────────────────────────────────────
    if best and {k[2:]: v for k, v in best.items() if k.startswith("P_")}:
        param_vals = {k[2:]: v for k, v in best.items() if k.startswith("P_")}
        print()
        print("  ── Winning PARAMS overrides (copy into your algo) ──")
        for k, v in sorted(param_vals.items()):
            print(f'    "{k}": {repr(v)},')
    print()


if __name__ == "__main__":
    main()
