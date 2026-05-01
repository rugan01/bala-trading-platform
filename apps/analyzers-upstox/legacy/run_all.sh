#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$SCRIPT_DIR"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
ENV_FILE="${BALA_TRADING_ENV_FILE:-$REPO_ROOT/.env}"
UNIVERSE_FILE="$ROOT/stock_fo_monitor/universe_nifty_fo.txt"

MCX_OUT="$REPO_ROOT/data/legacy-analyzers/mcx-monitor"
STOCK_FO_OUT="$REPO_ROOT/data/legacy-analyzers/stock-fo"
STOCK_INTRADAY_OUT="$REPO_ROOT/data/legacy-analyzers/stock-intraday"

mkdir -p "$MCX_OUT" "$STOCK_FO_OUT" "$STOCK_INTRADAY_OUT"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE"
  echo "Create it from $REPO_ROOT/.env.example or set BALA_TRADING_ENV_FILE."
  exit 1
fi

python3.11 "$ROOT/mcx_monitor/analyze_mcx.py"   --env-file "$ENV_FILE"   --output-dir "$MCX_OUT"   --max-risk-rupees 2000

python3.11 "$ROOT/stock_fo_monitor/analyze_stock_fo.py"   --env-file "$ENV_FILE"   --universe-file "$UNIVERSE_FILE"   --output-dir "$STOCK_FO_OUT"   --mode weekly   --top 8

python3.11 "$ROOT/stock_fo_monitor/analyze_stock_fo.py"   --env-file "$ENV_FILE"   --universe-file "$UNIVERSE_FILE"   --output-dir "$STOCK_FO_OUT"   --mode monthly   --top 8

python3.11 "$ROOT/stock_intraday_monitor/analyze_intraday.py"   --env-file "$ENV_FILE"   --universe-file "$UNIVERSE_FILE"   --output-dir "$STOCK_INTRADAY_OUT"   --top 8

echo "All reports generated under $REPO_ROOT/data/legacy-analyzers"
