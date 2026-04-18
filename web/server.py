#!/usr/bin/env python3
"""
IMC Prosperity Local Backtester Dashboard — backend server
"""

import os, sys, csv, json, uuid, shutil, subprocess, math
from pathlib import Path
from io import StringIO
from flask import Flask, request, jsonify, render_template

REPO_ROOT  = Path(__file__).parent.parent.resolve()
RUNS_DIR   = REPO_ROOT / "runs"
WEB_DIR    = Path(__file__).parent.resolve()
STORE_FILE = WEB_DIR / "runs_store.json"

app = Flask(__name__, template_folder=str(WEB_DIR / "templates"))

PALETTE = ["#1f77b4","#ff7f0e","#2ca02c","#d62728",
           "#9467bd","#8c564b","#e377c2","#7f7f7f",
           "#bcbd22","#17becf","#aec7e8","#ffbb78"]

# ── Helpers ────────────────────────────────────────────────────────────────────

def load_store():
    if STORE_FILE.exists():
        try: return json.loads(STORE_FILE.read_text())
        except: pass
    return {"runs": []}

def save_store(s):
    STORE_FILE.write_text(json.dumps(s, indent=2))

def find_backtester():
    for c in [
        REPO_ROOT/"target"/"release"/"rust_backtester",
        REPO_ROOT/"target"/"debug"/"rust_backtester",
        Path.home()/".cargo"/"bin"/"rust_backtester",
    ]:
        if c.exists(): return str(c)
    return shutil.which("rust_backtester")

def available_datasets():
    out = []
    ds_root = REPO_ROOT / "datasets"
    if not ds_root.exists(): return out
    for folder in sorted(ds_root.iterdir()):
        if not folder.is_dir(): continue
        csvs = list(folder.glob("prices_*.csv"))
        if csvs:
            days = sorted({int(c.stem.split("_day_")[1]) for c in csvs if "_day_" in c.stem})
            out.append({"id": folder.name, "days": days})
    return out

def next_color(store):
    used = {r.get("color") for r in store["runs"]}
    for c in PALETTE:
        if c not in used: return c
    return PALETTE[len(store["runs"]) % len(PALETTE)]

# ── Number helpers ─────────────────────────────────────────────────────────────

def _f(s):
    try: return float(s) if s and str(s).strip() else 0.0
    except: return 0.0

def _i(s):
    try: return int(float(s)) if s and str(s).strip() else 0
    except: return 0

# ── Parsing ────────────────────────────────────────────────────────────────────

def parse_activities_csv(raw: str) -> dict:
    """Parse activitiesLog CSV → {product: [row_dict, ...]}"""
    reader = csv.DictReader(StringIO(raw), delimiter=";")
    by_product: dict = {}
    for row in reader:
        p = row.get("product", "")
        if not p: continue
        bp1 = _f(row.get("bid_price_1"))
        bp2 = _f(row.get("bid_price_2"))
        bp3 = _f(row.get("bid_price_3"))
        bv1 = _i(row.get("bid_volume_1"))
        bv2 = _i(row.get("bid_volume_2"))
        bv3 = _i(row.get("bid_volume_3"))
        ap1 = _f(row.get("ask_price_1"))
        ap2 = _f(row.get("ask_price_2"))
        ap3 = _f(row.get("ask_price_3"))
        av1 = _i(row.get("ask_volume_1"))
        av2 = _i(row.get("ask_volume_2"))
        av3 = _i(row.get("ask_volume_3"))
        mid = _f(row.get("mid_price"))

        # Microprice (volume-weighted mid) — only when both sides exist
        tot = bv1 + av1
        micro = (ap1 * bv1 + bp1 * av1) / tot if (tot > 0 and bp1 > 0 and ap1 > 0) else mid

        spread   = (ap1 - bp1) if ap1 > 0 and bp1 > 0 else 0.0
        total_vol = bv1+bv2+bv3 + av1+av2+av3
        pressure  = (av1+av2+av3) / total_vol if total_vol > 0 else 0.5

        # has_ob: True when the product actually has order book at this tick
        has_ob = (bp1 > 0 or ap1 > 0)

        entry = {
            "day":    _i(row.get("day")),
            "ts":     _i(row.get("timestamp")),
            "mid":    mid if has_ob else None,
            "micro":  micro if has_ob else None,
            "pnl":    _f(row.get("profit_and_loss")),
            "spread": spread,
            "pressure": pressure,
            "has_ob": has_ob,
            "bp1": bp1, "bv1": bv1, "bp2": bp2, "bv2": bv2, "bp3": bp3, "bv3": bv3,
            "ap1": ap1, "av1": av1, "ap2": ap2, "av2": av2, "ap3": ap3, "av3": av3,
        }
        by_product.setdefault(p, []).append(entry)
    return by_product


def _gts(day, ts):
    """Global monotone timestamp: day=-2 → 8_000_000+ts, day=0 → 10_000_000+ts"""
    return (day + 10) * 1_000_000 + ts


def _downsample(lst, n):
    if len(lst) <= n: return lst
    step = len(lst) / n
    return [lst[int(i * step)] for i in range(n)]


def _max_drawdown(series):
    peak, dd = float("-inf"), 0.0
    for v in series:
        if v is None: continue
        peak = max(peak, v)
        dd   = max(dd, peak - v)
    return dd


def _volatility(series):
    """Std-dev of differences, ignoring None values."""
    vals = [v for v in series if v is not None]
    if len(vals) < 2: return 0.0
    diffs = [vals[i] - vals[i-1] for i in range(1, min(len(vals), 51))]
    if not diffs: return 0.0
    mu = sum(diffs) / len(diffs)
    return math.sqrt(sum((x - mu) ** 2 for x in diffs) / len(diffs))


def parse_submission_log(log_path: Path) -> dict:
    """Full parse of submission.log → chart-ready JSON for the frontend."""
    try:
        data = json.loads(log_path.read_text())
    except Exception as e:
        return {"error": str(e)}

    by_product    = parse_activities_csv(data.get("activitiesLog", ""))
    trade_history = data.get("tradeHistory", [])
    logs_raw      = data.get("logs", [])
    products      = sorted(by_product.keys())

    # ── Figure out which day this log covers (for trade gts computation) ──────
    # The tradeHistory rows from the Rust backtester do NOT have a "day" field,
    # so we derive it from the activitiesLog (which always has it).
    dominant_day = 0
    for rows in by_product.values():
        if rows:
            dominant_day = rows[0]["day"]
            break

    # ── Build global-timestamp index: product → {gts: row} ────────────────────
    all_gts: set[int] = set()
    gts_map: dict[str, list[int]] = {}
    idx:     dict[str, dict[int, dict]] = {}

    for p, rows in by_product.items():
        gts_list = [_gts(r["day"], r["ts"]) for r in rows]
        gts_map[p] = gts_list
        idx[p]     = {g: r for g, r in zip(gts_list, rows)}
        all_gts.update(gts_list)

    all_gts_sorted = sorted(all_gts)
    sampled        = _downsample(all_gts_sorted, 4000)

    # ── Build sampled series arrays ────────────────────────────────────────────
    # Rules:
    #   • bid/ask/mid/micro: output None when the product has no OB at that tick
    #     (prevents zero-noise lines connecting across data gaps)
    #   • pnl: carry-forward the last valid (has_ob) PnL to avoid mark-to-market
    #     spikes that occur when mid_price = 0 and position is non-zero
    #   • orderbook arrays: output 0s when no OB (used for the live OB display)

    last_ob_row: dict[str, dict | None] = {p: None for p in products}
    last_valid_pnl: dict[str, float]    = {p: 0.0  for p in products}

    pnl_s    = {p: [] for p in products}
    mid_s    = {p: [] for p in products}
    micro_s  = {p: [] for p in products}
    spread_s = {p: [] for p in products}
    bid_s    = {p: [] for p in products}
    ask_s    = {p: [] for p in products}
    press_s  = {p: [] for p in products}
    ob       = {p: {k: [] for k in
                    ["bp1","bv1","bp2","bv2","bp3","bv3",
                     "ap1","av1","ap2","av2","ap3","av3"]}
                for p in products}

    sampled_days: list[int] = []
    total_pnl:    list[float] = []

    for g in sampled:
        # Representative day for this tick
        rep_day = dominant_day
        for p in products:
            if g in idx[p]:
                rep_day = idx[p][g]["day"]
                break
        sampled_days.append(rep_day)

        tick_total = 0.0
        for p in products:
            row = idx[p].get(g)          # row at this exact tick, or None

            if row is not None:
                if row["has_ob"]:
                    last_ob_row[p]     = row
                    last_valid_pnl[p]  = row["pnl"]

            # ── PnL: always carry-forward last valid value ─────────────────
            pnl_s[p].append(last_valid_pnl[p])
            tick_total += last_valid_pnl[p]

            # ── Price series: only output when this tick has a real OB ──────
            if row is not None and row["has_ob"]:
                bid_s[p].append(row["bp1"]   if row["bp1"] > 0   else None)
                ask_s[p].append(row["ap1"]   if row["ap1"] > 0   else None)
                mid_s[p].append(row["mid"])
                micro_s[p].append(row["micro"])
                spread_s[p].append(row["spread"])
                press_s[p].append(row["pressure"])
                for k in ["bp1","bv1","bp2","bv2","bp3","bv3",
                           "ap1","av1","ap2","av2","ap3","av3"]:
                    ob[p][k].append(row[k])
            else:
                # No OB → None gaps in price chart; 0s in OB display
                bid_s[p].append(None)
                ask_s[p].append(None)
                mid_s[p].append(None)
                micro_s[p].append(None)
                spread_s[p].append(None)
                r_last = last_ob_row[p]
                press_s[p].append(r_last["pressure"] if r_last else 0.5)
                for k in ["bp1","bv1","bp2","bv2","bp3","bv3",
                           "ap1","av1","ap2","av2","ap3","av3"]:
                    ob[p][k].append(0)

        total_pnl.append(tick_total)

    # ── Positions from tradeHistory ────────────────────────────────────────────
    # tradeHistory rows don't have "day" → use dominant_day derived above.
    # We accumulate position using a sorted pointer sweep (correctly handles
    # trade timestamps that fall between sampled ticks).
    all_trades_out: list[dict] = []

    for t in trade_history:
        sym    = t.get("symbol", "")
        price  = _f(t.get("price"))
        qty    = _i(t.get("quantity"))
        buyer  = t.get("buyer", "")
        seller = t.get("seller", "")
        # day field absent in backtester output → use dominant_day
        day_t  = _i(t.get("day")) if t.get("day") is not None else dominant_day
        ts_t   = _i(t.get("timestamp"))
        g      = _gts(day_t, ts_t)

        if buyer == "SUBMISSION":    side = "buy"
        elif seller == "SUBMISSION": side = "sell"
        else:                        side = "market"

        all_trades_out.append({
            "gts": g, "ts": ts_t, "day": day_t,
            "symbol": sym, "side": side,
            "price": price, "quantity": qty,
        })

    # Sort trades by gts for the pointer sweep
    own_trades = [t for t in all_trades_out if t["side"] != "market"]
    own_trades.sort(key=lambda t: t["gts"])

    pos_s:       dict[str, list] = {p: [] for p in products}
    running_pos: dict[str, int]  = {p: 0  for p in products}
    ptr = 0

    for g in sampled:
        # Apply all own trades up to and including this sampled timestamp
        while ptr < len(own_trades) and own_trades[ptr]["gts"] <= g:
            ev = own_trades[ptr]
            p2 = ev["symbol"]
            if p2 in running_pos:
                if ev["side"] == "buy":
                    running_pos[p2] += ev["quantity"]
                else:
                    running_pos[p2] -= ev["quantity"]
            ptr += 1
        for p in products:
            pos_s[p].append(running_pos[p])

    # ── Metrics ────────────────────────────────────────────────────────────────
    max_dd = _max_drawdown(total_pnl)
    vol    = {p: _volatility(mid_s[p]) for p in products}

    # ── Algorithm logs ────────────────────────────────────────────────────────
    algo_logs = []
    for entry in logs_raw:
        txt = entry.get("lambdaLog", "").strip()
        sb  = entry.get("sandboxLog", "").strip()
        if txt or sb:
            algo_logs.append({
                "day": entry.get("day"),
                "ts":  _i(entry.get("timestamp") or 0),
                "log": txt, "sys": sb,
            })
    algo_logs = algo_logs[-300:]

    return {
        "products":      products,
        "timestamps":    sampled,
        "days":          sampled_days,
        "total_pnl":     total_pnl,
        "max_drawdown":  max_dd,
        "pnl_series":    pnl_s,
        "mid_series":    mid_s,
        "micro_series":  micro_s,
        "spread_series": spread_s,
        "bid_series":    bid_s,
        "ask_series":    ask_s,
        "pressure_series": press_s,
        "pos_series":    pos_s,
        "orderbook":     ob,
        "volatility":    vol,
        "trades":        all_trades_out,
        "algo_logs":     algo_logs,
    }


def _merge(base: dict, extra: dict) -> None:
    """
    Merge extra day's parsed data into base (in-place).
    IMPORTANT: Do NOT add a timestamp offset — _gts() already produces
    globally-unique timestamps (day=-2 → 8M range, day=-1 → 9M, day=0 → 10M).
    Adding an offset here would shift already-correct values and produce
    wrong x-axis values (e.g. 8M→18M→29M instead of 8M→9M→10M).
    """
    if not base.get("timestamps") or not extra.get("timestamps"):
        return

    # Simple concatenation — data is already in chronological order
    base["timestamps"].extend(extra["timestamps"])
    base["days"].extend(extra["days"])
    base["total_pnl"].extend(extra["total_pnl"])
    base["max_drawdown"] = _max_drawdown(base["total_pnl"])

    for p in extra["products"]:
        if p not in base["products"]:
            base["products"].append(p)
        for k in ("pnl_series", "mid_series", "micro_series", "spread_series",
                  "bid_series", "ask_series", "pressure_series", "pos_series"):
            base[k].setdefault(p, []).extend(extra[k].get(p, []))
        ob_base = base["orderbook"].setdefault(p, {k: [] for k in
            ["bp1","bv1","bp2","bv2","bp3","bv3","ap1","av1","ap2","av2","ap3","av3"]})
        ob_extra = extra["orderbook"].get(p, {})
        for k in ob_base:
            ob_base[k].extend(ob_extra.get(k, []))

    base["trades"].extend(extra["trades"])
    base["algo_logs"].extend(extra["algo_logs"])

    # Merge volatility (take max across days as a proxy)
    for p in extra.get("volatility", {}):
        base["volatility"][p] = max(
            base["volatility"].get(p, 0),
            extra["volatility"].get(p, 0)
        )


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def status():
    bt = find_backtester()
    return jsonify({"ok": bt is not None, "backtester": bt,
                    "datasets": available_datasets(), "python": sys.executable})


@app.route("/api/runs")
def get_runs():
    store = load_store()
    for r in store["runs"]:
        # Enrich with metrics if not already present
        for rd in RUNS_DIR.glob(f"{r['run_id']}*"):
            mf = rd / "metrics.json"
            if mf.exists() and "final_pnl" not in r:
                try:
                    m = json.loads(mf.read_text())
                    r["final_pnl"]             = m.get("final_pnl_total", 0)
                    r["final_pnl_by_product"]  = m.get("final_pnl_by_product", {})
                    r["tick_count"]            = m.get("tick_count", 0)
                    r["own_trade_count"]       = m.get("own_trade_count", 0)
                except: pass
                break
    return jsonify(store["runs"])


@app.route("/api/runs/<run_id>", methods=["DELETE"])
def delete_run(run_id):
    s = load_store()
    s["runs"] = [r for r in s["runs"] if r["run_id"] != run_id]
    save_store(s)
    # Delete all run directories on disk that belong to this run_id
    for d in RUNS_DIR.glob(f"{run_id}*"):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
    return jsonify({"ok": True})


@app.route("/api/runs/<run_id>/data")
def get_run_data(run_id):
    def day_sort_key(p: Path) -> int:
        name = p.name
        if "-d" in name:
            try: return int(name.split("-d")[-1])
            except: pass
        return 999

    day_dirs = sorted(
        [d for d in RUNS_DIR.glob(f"{run_id}*") if d.is_dir()],
        key=day_sort_key
    )
    if not day_dirs:
        return jsonify({"error": f"No run directory for {run_id}"}), 404

    merged = None
    for d in day_dirs:
        lp = d / "submission.log"
        if lp.exists():
            parsed = parse_submission_log(lp)
            if "error" in parsed:
                continue
            if merged is None:
                merged = parsed
            else:
                _merge(merged, parsed)

    if merged is None:
        return jsonify({"error": "No submission.log found"}), 404
    return jsonify(merged)


@app.route("/api/backtest", methods=["POST"])
def run_backtest():
    bt = find_backtester()
    if not bt:
        return jsonify({"error":
            "rust_backtester not found.\n"
            "Install Rust first:  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh\n"
            "Then:                source $HOME/.cargo/env\n"
            "Then:                ./scripts/cargo_local.sh install --path ."
        }), 500

    f       = request.files.get("trader")
    dataset = request.form.get("dataset", "round1")
    day     = request.form.get("day", "")
    name    = request.form.get("name", "Algorithm")
    if not f:                             return jsonify({"error": "No trader file"}), 400
    if not f.filename.endswith(".py"):    return jsonify({"error": "File must be .py"}), 400

    run_id   = uuid.uuid4().hex[:10]
    tmp_path = Path(f"/tmp/trader_{run_id}.py")
    f.save(str(tmp_path))

    try:
        cmd = [bt, "--trader", str(tmp_path), "--dataset", dataset, "--run-id", run_id]
        if day:
            # Use --day=VALUE format so negative numbers (e.g. -2) aren't
            # misinterpreted as CLI flags by the argument parser
            cmd += [f"--day={day}"]
        proc = subprocess.run(
            cmd, cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=600,
            env={**os.environ, "PYO3_PYTHON": sys.executable}
        )
        if proc.returncode != 0:
            return jsonify({
                "error":  f"Backtester failed (exit {proc.returncode})",
                "stderr": proc.stderr.strip(),
                "stdout": proc.stdout.strip(),
            }), 500

        # Read metrics from the first matching directory
        metrics = {}
        for rd in sorted(RUNS_DIR.glob(f"{run_id}*"), key=lambda p: len(p.name)):
            mf = rd / "metrics.json"
            if mf.exists():
                try: metrics = json.loads(mf.read_text()); break
                except: pass

        store = load_store()
        color = next_color(store)
        meta  = {
            "run_id":              run_id,
            "name":                name,
            "dataset":             dataset,
            "day":                 day,
            "color":               color,
            "visible":             True,
            "filename":            f.filename,
            "final_pnl":           metrics.get("final_pnl_total", 0),
            "final_pnl_by_product":metrics.get("final_pnl_by_product", {}),
            "tick_count":          metrics.get("tick_count", 0),
            "own_trade_count":     metrics.get("own_trade_count", 0),
            "backtester_output":   proc.stdout.strip(),
        }
        store["runs"].append(meta)
        save_store(store)
        return jsonify(meta)
    finally:
        tmp_path.unlink(missing_ok=True)


@app.route("/api/runs/<run_id>/toggle", methods=["POST"])
def toggle_run(run_id):
    s = load_store()
    for r in s["runs"]:
        if r["run_id"] == run_id:
            r["visible"] = not r.get("visible", True)
            break
    save_store(s)
    return jsonify({"ok": True})


@app.route("/api/runs/<run_id>/rename", methods=["POST"])
def rename_run(run_id):
    name = (request.json or {}).get("name", "").strip()
    if not name: return jsonify({"error": "empty name"}), 400
    s = load_store()
    for r in s["runs"]:
        if r["run_id"] == run_id:
            r["name"] = name
            break
    save_store(s)
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"\n  IMC Prosperity Local Backtester")
    print(f"  ─────────────────────────────────")
    bt = find_backtester()
    print(f"  Backtester : {bt or '*** NOT FOUND — run make install ***'}")
    print(f"  Open       : http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
