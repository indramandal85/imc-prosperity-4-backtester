#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# IMC Prosperity Local Backtester — start script
# Run from the repo root:  ./web/start.sh
# Or from web/ folder:     ./start.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")/.."   # always run from repo root

PYTHON="${PYTHON:-python3}"

# 1. Install Flask if missing
if ! "$PYTHON" -c "import flask" 2>/dev/null; then
  echo "→ Installing Flask…"
  "$PYTHON" -m pip install --quiet flask
fi

# 2. Check for the backtester binary
if command -v rust_backtester &>/dev/null; then
  echo "✓ rust_backtester found at: $(command -v rust_backtester)"
else
  # Also check local build dirs
  if [ -f "target/release/rust_backtester" ] || [ -f "target/debug/rust_backtester" ]; then
    echo "✓ rust_backtester found in target/"
  else
    echo ""
    echo "⚠  rust_backtester binary not found."
    echo "   Build it first with ONE of:"
    echo "     make install          (installs to ~/.cargo/bin)"
    echo "     make build-release    (builds to target/release/)"
    echo "     ./scripts/cargo_local.sh install --path ."
    echo ""
    echo "   Continuing anyway — you can still start the UI."
    echo ""
  fi
fi

# 3. Start the server
echo "→ Starting dashboard on http://localhost:8000"
exec "$PYTHON" web/server.py "$@"
