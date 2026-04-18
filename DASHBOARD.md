# IMC Prosperity — Local Backtester Dashboard

A fully local, interactive backtesting dashboard for IMC Prosperity 4. Upload your Python trader, run it against any round dataset, and get professional-grade interactive charts — all on your machine, nothing sent anywhere.

---

## Preview

### Main Dashboard — ALL PRODUCTS view
![Dashboard Overview](Sample_image/Front_page1.png)

### Price & Liquidity — per-product view
![Price and Liquidity](Sample_image/Front_page2.png)

### PnL Performance & Position — multi-run comparison
![PnL and Position Charts](Sample_image/chart1.png)

### Right panel — Strategy Management, Order Book, Market Dynamics
![Order Book and Panels](Sample_image/order_book.png)

---

## Features

- **Interactive charts** — Price & Liquidity, PnL Performance, Position
- **Multi-run comparison** — keep multiple algorithms visible side by side
- **Tick-by-tick playback** — scrub through the backtest with a slider
- **Live Order Book** — updates as you scrub through ticks
- **Product filters** — switch between ALL PRODUCTS and individual assets
- **Expand to full screen** — any chart can be expanded
- **Trackpad scroll zoom** on all charts
- **No internet required** — fully local, no data leaves your machine

---

## Requirements

- **Python 3.9+**
- **Rust + Cargo** (for building the backtester binary)
- **Flask 3.0+** (auto-installed by the start script)

---

## Setup & Installation

### 1. Clone the repository

```bash
git clone https://github.com/GeyzsoN/prosperity_rust_backtester.git
cd prosperity_rust_backtester
```

### 2. Install Rust (first time only)

**macOS / Linux:**
```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"
```

**Windows:** Use WSL2 (Ubuntu) and run the same command above.

Verify:
```bash
rustc --version
cargo --version
```

### 3. Build the backtester binary

```bash
# Recommended — installs to ~/.cargo/bin (available system-wide)
make install
```

If `make` is unavailable:
```bash
./scripts/cargo_local.sh install --path .
```

Verify:
```bash
rust_backtester --version
```

> **macOS build hang?** If the build stalls while `syspolicyd` spikes CPU:
> ```bash
> sudo killall syspolicyd && make build-release
> ```

### 4. Start the dashboard

```bash
./web/start.sh
```

Open **http://localhost:8000** in your browser. Flask is auto-installed if missing.

To use a different port:
```bash
PORT=8080 ./web/start.sh
```

---

## How to Use

### Step 1 — Upload your algorithm

Click **Upload Algorithm** in the top-right corner.

![Upload Modal](Sample_image/upload.png)

- Drop or browse to your `.py` trader file
- Your file must contain `class Trader` with a `run(state)` method
- Give the run a name, select a dataset (Round 1 / Tutorial) and a day
- Click **▶ Run Backtest**

### Step 2 — Explore the dashboard

Once the backtest finishes, charts load automatically.

**Product filter buttons** at the top switch all charts at once:
- **ALL PRODUCTS** → shows total PnL with per-product breakdown and combined position chart
- **ASH_COATED_OSMIUM / INTARIAN_PEPPER_ROOT** → shows Price & Liquidity, PnL, and Position for that asset

**Playback bar** (always visible at the top):
- Drag the slider to scrub tick by tick
- Use ▶ / ⏸ to play/pause, and 1×–20× to set speed
- The Order Book and stat cards update live as you scrub

**Price chart hover:**
- Move across the chart to see Ask / Mid / Microprice / Bid values in a unified tooltip
- Hover near a **buy or sell dot** to see fill details (side, quantity, price)

### Step 3 — Compare algorithms

The **Strategy Management** panel on the right shows all your runs.

- Click **COMPARE** to overlay another run on the PnL and Position charts
- Click a run row to make it the active run (drives the price chart and order book)
- Click **×** to remove a run

---

## Folder Structure

```
prosperity_rust_backtester/
├── web/
│   ├── server.py           # Flask backend — parses logs, serves API
│   ├── start.sh            # One-command launcher
│   └── templates/
│       └── index.html      # Dashboard frontend (Plotly.js)
├── datasets/
│   ├── tutorial/           # Tutorial round CSVs
│   ├── round1/             # Round 1 CSVs (days -2, -1, 0)
│   └── round2/ … round8/   # Placeholders for future rounds
├── runs/                   # Backtest outputs (auto-created)
│   └── <run-id>/
│       ├── submission.log  # Parsed by the dashboard
│       └── metrics.json    # Summary metrics
├── src/                    # Rust backtester source
├── traders/
│   └── latest_trader.py    # Bundled example trader
├── Sample_image/           # Dashboard screenshots
└── Makefile
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `rust_backtester: command not found` | Run `source "$HOME/.cargo/env"` then retry |
| Flask import error | Run `python3 -m pip install flask` |
| Charts blank after reload | Restart the server: `Ctrl+C` then `./web/start.sh` |
| Port 8000 already in use | `PORT=8080 ./web/start.sh` |
| macOS Gatekeeper blocks binary | `xattr -d com.apple.quarantine target/release/rust_backtester` |

---

## License

Dual-licensed under [Apache-2.0](LICENSE-APACHE) and [MIT](LICENSE-MIT).
