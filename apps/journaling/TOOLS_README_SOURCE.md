# Tools

Scripts, utilities, and automations for the Product OS.

---

## Active Tools

### Broker Trade Backfill (`broker_trade_backfill.py`)

Repairs Notion trading-journal rows from Upstox broker XLSX exports when the normal same-day trade pull was missed.

What it does:
- parses the broker export directly from the XLSX file
- fills exact `Entry Time` and `Exit Time` in Notion
- can reconcile intraday closed rows when historical Upstox processing got direction or sequencing wrong
- can clean stale `Trade Label` direction text in the same pass
- can create genuinely missing journal rows for missed days or multi-day backfills
- writes a stable `Journal Key` rich-text property for safer reruns and dedupe
- reruns are idempotent and should skip unchanged rows

Why this exists:
- same-day Upstox trade APIs include timestamps and `order_id`
- past-date Upstox historical trades do not expose a documented exact trade-time field
- past-date historical rows also do not expose `order_id`, so exact time recovery from Upstox alone is not reliable

Run:
```bash
~/balas-product-os/Tools/.venv/bin/python ~/balas-product-os/Tools/broker_trade_backfill.py \
  --broker-file /path/to/trade_export.xlsx \
  --date YYYY-MM-DD
```

Multiple files:
```bash
~/balas-product-os/Tools/.venv/bin/python ~/balas-product-os/Tools/broker_trade_backfill.py \
  --broker-file /path/to/nimmy_export.xlsx \
  --broker-file /path/to/bala_export.xlsx \
  --date YYYY-MM-DD
```

Recommended workflow:
1. Preferred: run `trade_journaling.py` on the same trading day after market close.
2. If that was missed, export the broker trade file from the Upstox terminal.
3. Run `broker_trade_backfill.py` to repair existing rows and create any missing rows safely.
4. For week-long misses, pass one or more broker files; the script processes dates chronologically.

Important note:
- dry-run is reliable for syntax and most reconciliation checks, but on rows that do not yet have the new `Journal Key`, preview mode can overstate "would create" because it does not persist the key first
- the real run is safer than the preview in that specific situation

### Upstox Token Refresh (`upstox_token_refresh.py`)

Automatically refreshes the Upstox API access token using TOTP-based authentication.

**How it works:**
1. Reads credentials from `~/.balas-product-os/.env`
2. Authenticates via Mobile → Password → TOTP → PIN
3. Obtains fresh access token from Upstox OAuth
4. Updates `.env` with new token and timestamp
5. Sends macOS notification on success/failure

**Schedule:** Daily at 8:00 AM via launchd (also runs on wake if missed)

**Dependencies:**
- Python 3.12+ (uses `.venv` with Python 3.13)
- `upstox-totp` library ([GitHub](https://github.com/batpool/upstox-totp))
- `python-dotenv`

**Files:**
| File | Purpose |
|------|---------|
| `upstox_token_refresh.py` | Main script |
| `com.bala.upstox-token-refresh.plist` | launchd scheduler config |
| `install_token_scheduler.sh` | One-time installation script |
| `.venv/` | Python virtual environment |

**Commands:**
```bash
# Manual refresh with notification
./upstox_token_refresh.py --notify

# View logs
tail -f ~/Library/Logs/upstox_token_refresh.log

# Check scheduler status
launchctl list | grep upstox

# Stop scheduler
launchctl unload ~/Library/LaunchAgents/com.bala.upstox-token-refresh.plist

# Start scheduler
launchctl load ~/Library/LaunchAgents/com.bala.upstox-token-refresh.plist
```

**Environment Variables Required:**
```
UPSTOX_CLIENT_ID       # API Key from Upstox Developer Console
UPSTOX_CLIENT_SECRET   # API Secret
UPSTOX_REDIRECT_URI    # OAuth callback URL
UPSTOX_USERNAME        # 10-digit mobile number
UPSTOX_PASSWORD        # Account password
UPSTOX_PIN_CODE        # 6-digit PIN
UPSTOX_TOTP_SECRET     # TOTP secret key (from 2FA setup)
```

**Troubleshooting:**
- If TOTP fails, verify your authenticator app shows the same code
- If login fails, check password hasn't changed
- Token expires at market close (~3:30 PM IST) - refresh before trading
- Logs at `~/Library/Logs/upstox_token_refresh.log`

### Walk-Forward Validator (`walk_forward/`)

Paper-trades the `SILVERMIC V3 CPR Band TC/BC Rejection` strategy during live MCX hours.

What it does:
- auto-discovers the active `SILVERMIC` front-month futures contract
- computes CPR from previous-day OHLC
- monitors live `15m` candles
- paper-trades signals only
- logs entries/exits to Notion
- sends Telegram alerts and end-of-day summary

Key docs:
- [walk_forward/README.md](/Users/rugan/balas-product-os/Tools/walk_forward/README.md)
- [walk_forward/LLM_HANDOVER.md](/Users/rugan/balas-product-os/Tools/walk_forward/LLM_HANDOVER.md)
- [walk_forward/ROADMAP.md](/Users/rugan/balas-product-os/Tools/walk_forward/ROADMAP.md)

Run:
```bash
cd ~/balas-product-os/Tools/walk_forward
source ../.venv/bin/activate
python main.py
```

Important notes:
- this validator is **paper-only**
- `--dry-run` uses mock Notion/Telegram writers, so it does not validate end-to-end alerting/journaling
- the runtime was repaired on April 15, 2026 to use the newer Upstox `market-quote` MCX instrument master and `v3/historical-candle`
- the April 17 Phase 2 slices added runtime composition, normalized models, provider/strategy interfaces, strategy selection through `WFV_STRATEGY_ID` or `--strategy-id`, position-plan selection through `WFV_POSITION_PLAN_ID` or `--position-plan-id`, a replay/backtest-compatible runner through `walk_forward/replay.py`, replay JSON/CSV metrics under `walk_forward/output/replay`, and batch replay comparisons through `walk_forward/replay_batch.py`
- longer-term strategy/broker-agnostic evolution continues in the walk-forward roadmap

---

## Scheduled Tasks

| Task | Schedule | Plist Location |
|------|----------|----------------|
| Upstox Token Refresh | 8:00 AM daily + on wake | `~/Library/LaunchAgents/com.bala.upstox-token-refresh.plist` |

**Note on Sleep Mode:**
macOS launchd does NOT run scheduled tasks while the system is asleep. The scheduler is configured to:
1. Run at 8:00 AM if awake
2. Run immediately on wake if the 8 AM run was missed

---

## Planned Tools

- `upstox-trade-pull.py` — Pull executed trades from Upstox API
- `sheets-push.py` — Push formatted data to Google Sheets
- `rs-screener.py` — Relative strength screener for F&O stocks vs Nifty
- `pivot-calculator.py` — Daily CPR and pivot level calculator
- `straddle-monitor.py` — ATM straddle premium tracker for weekly expiry strategy

---

## How to Add New Tools

1. Create the script in this folder
2. Document usage in a comment block at the top
3. If scheduled, create a `.plist` file and add to LaunchAgents
4. Update this README with tool documentation
5. If it uses secrets, document in `_Registry/SECRETS.md`
