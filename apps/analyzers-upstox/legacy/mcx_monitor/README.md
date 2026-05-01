# MCX 15-Minute Monitor

This folder contains a small local automation bundle for monitoring MCX commodity futures using Upstox.

## Files

- `analyze_mcx.py`
  - Reads the Upstox access token from your `.env`
  - Fetches the nearest futures contracts for:
    - `GOLDM`
    - `SILVERMIC`
    - `CRUDEOILM`
    - `ZINCMINI`
    - `NATGASMINI`
  - Pulls live futures quotes from Upstox
  - Pulls recent `15m` candles and recent daily candles from Upstox
  - Scores each commodity as bullish, bearish, or neutral
- Builds a practical trade plan with:
    - initial stop loss
    - first target
    - next key level / second target
    - fast supertrend trail level
    - whether the first target is still realistic inside the current day's range
    - lot-size-based rupee risk for 1 lot and 2 lots
    - an `actionable` flag that respects your risk cap
  - Compares the current run against the previous saved run
  - Writes:
    - `latest_report.md`
    - `state.json`

- `com.rugan.mcx-monitor.plist`
  - `launchd` job definition for macOS
  - Runs the script every `900` seconds, which is every `15` minutes

## What "hypothesis gets stronger or weaker" means

Each run creates a score based on:

- price vs previous close
- price vs open
- price vs average traded price
- price location inside the current day's range
- net change

The script then compares that score with the previous run stored in `state.json`.

Examples:

- score goes from `2` to `4`
  - thesis is strengthening
- score goes from `4` to `1`
  - thesis is weakening
- score stays the same
  - thesis is unchanged

## How stop loss and target are decided

For bullish setups:

- initial stop is based on the more conservative support reference from:
  - previous `15m` swing low
  - `Supertrend(5,3)`
- first target is based on the smaller of:
  - the `1:1` risk-reward level
  - the next key level when available

For bearish setups:

- initial stop is based on the more conservative resistance reference from:
  - previous `15m` swing high
  - `Supertrend(5,3)`
- first target is based on the smaller move needed between:
  - the `1:1` risk-reward level
  - the next downside key level when available

Trade management assumption:

- always `2` lots
- book `1` lot at `T1`
- trail the remaining `1` lot using `Supertrend(5,1.5)`

## How risk filtering works

The script converts stop distance into rupee risk using the futures P&L multiplier per 1-point price move.

Example:

- if crude stop distance is `70` points
- and crude lot size is `10`
- then:
  - `1` lot risk = `700`
  - `2` lot risk = `1400`

For the tracked contracts, the configured multipliers are:

- `GOLDM`: `10`
- `SILVERMIC`: `1`
- `CRUDEOILM`: `10`
- `ZINCMINI`: `1000`
- `NATGASMINI`: `250`

A setup is marked `actionable` only if:

- bias is bullish or bearish
- the `2` lot rupee risk is within your configured cap
- and `T1` is still realistic inside today's current range

The script also calculates:

- `allowed lots`
  - how many lots fit inside the rupee risk cap
- `Status`
  - `Trade`
  - `Reduce size`
  - `Skip`

Default cap:

- `Rs 2,000`

You can override it:

```bash
python3.11 "/Users/rugan/Projects/upstox-analyzer/mcx_monitor/analyze_mcx.py" \
  --env-file "/Users/rugan/balas-product-os/.env" \
  --output-dir "/Users/rugan/Projects/upstox-analyzer/output/mcx-monitor" \
  --max-risk-rupees 2000
```

## Manual test

Run this first before setting up `launchd`:

```bash
python3.11 "/Users/rugan/Projects/upstox-analyzer/mcx_monitor/analyze_mcx.py" \
  --env-file "/Users/rugan/balas-product-os/.env" \
  --output-dir "/Users/rugan/Projects/upstox-analyzer/output/mcx-monitor"
```

## Output files

After one successful run, check:

- `/Users/rugan/Projects/upstox-analyzer/output/mcx-monitor/latest_report.md`
- `/Users/rugan/Projects/upstox-analyzer/output/mcx-monitor/state.json`

The report now includes:

- direction
- entry idea
- stop loss
- first target
- second target / trail level
- whether the first target is still possible within today's current range
- lot size
- risk for `1` lot
- risk for `2` lots
- allowed lots under the configured risk cap
- status: `Trade`, `Reduce size`, or `Skip`
- whether the setup is actually tradable under the risk cap

## launchd install steps

1. Create the output folder:

```bash
mkdir -p "/Users/rugan/Projects/upstox-analyzer/output/mcx-monitor"
```

2. Copy the plist into LaunchAgents:

```bash
mkdir -p "$HOME/Library/LaunchAgents"
cp "/Users/rugan/Projects/upstox-analyzer/mcx_monitor/com.rugan.mcx-monitor.plist" \
  "$HOME/Library/LaunchAgents/com.rugan.mcx-monitor.plist"
```

3. Load it:

```bash
launchctl unload "$HOME/Library/LaunchAgents/com.rugan.mcx-monitor.plist" 2>/dev/null || true
launchctl load "$HOME/Library/LaunchAgents/com.rugan.mcx-monitor.plist"
```

4. Check that it is loaded:

```bash
launchctl list | grep mcx-monitor
```

## Logs

- stdout:
  - `/Users/rugan/Projects/upstox-analyzer/output/mcx-monitor/launchd.stdout.log`
- stderr:
  - `/Users/rugan/Projects/upstox-analyzer/output/mcx-monitor/launchd.stderr.log`

## Troubleshooting

If you see an error like:

```text
No live quote returned for MCX_FO|...
```

that usually means Upstox returned quote rows keyed by trading symbol text instead of the raw `instrument_key`.

The current script already handles:

- raw `instrument_key`
- `instrument_key` with `:` instead of `|`
- trading-symbol style keys such as `MCX_FO:GOLDM26MAYFUT`
- rows where the match is only available through `instrument_token`

So if this error appears again, it usually means the API payload shape changed and the quote-matching helper in `analyze_mcx.py` needs to be updated.

## Customization

You can change:

- the `.env` path in the plist
- the output directory in the plist
- the watchlist in `analyze_mcx.py`
- the 15-minute schedule by changing:
  - `StartInterval` from `900` to another number of seconds

## Important note

This script is a screening and monitoring tool. It does not place trades.
