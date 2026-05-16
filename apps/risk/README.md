# MTM Guard

Account-wide MTM monitor with Telegram broadcasting and guarded control actions.

Current implementation:
- polls Upstox short-term positions for one account at a time
- posts MTM heartbeats to a Telegram alert chat/channel every 5 minutes by default
- accepts private Telegram bot commands from whitelisted user IDs
- requires double confirmation before `Close All Positions`
- auto-refreshes stale Upstox tokens once on `401`

## Environment

Set these locally in `.env`:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_ALERT_CHAT_ID=
TELEGRAM_CONTROL_CHAT_IDS=
TELEGRAM_ALLOWED_USER_IDS=
```

Notes:
- `TELEGRAM_ALERT_CHAT_ID` falls back to `TELEGRAM_CHAT_ID`
- `TELEGRAM_ALLOWED_USER_IDS` is required for command/control
- `TELEGRAM_CONTROL_CHAT_IDS` is optional but recommended for extra safety

## Run

```bash
cd /Users/rugan/Projects/bala-trading-platform
./.venv/bin/python apps/risk/mtm_guard.py --account BALA --profit-target 5000 --loss-limit 3000
```

Read-only status check:

```bash
./.venv/bin/python apps/risk/mtm_guard.py --account BALA --once
```

Print recent Telegram identity values for control bootstrap:

```bash
./.venv/bin/python apps/risk/mtm_guard.py --account BALA --print-telegram-identities
```

Dry-run close protection for testing:

```bash
./.venv/bin/python apps/risk/mtm_guard.py --account BALA --dry-run-close
```

## Telegram commands

Send these from a private bot chat or a whitelisted admin group:

```text
/status
/set limits 5000 3000
/set profit 5000
/set loss 3000
/pause
/resume
/stop
/close
```

You can also include the account explicitly:

```text
/status BALA
/set BALA limits 5000 3000
/close BALA
```

State-changing commands require confirmation.

Notes:
- `/pause` keeps the process running but stops the active guard behavior
- `/stop` terminates the running MTM guard Python process after confirmation
- if you later run the guard under a supervisor such as `launchd` with automatic restart, `/stop` may only stop the current process instance; `/pause` is the safer durable control inside the service

## Current safety model

- updates can go to a broadcast channel
- commands only work for allowed Telegram user IDs
- close-all always needs a second confirm click
- if `TELEGRAM_ALLOWED_USER_IDS` is missing, control commands are disabled
- Upstox flatten uses the official account-wide exit-all positions endpoint

## Runtime state

State is stored under:

`data/runtime/risk/mtm_guard_<account>.json`

The file keeps:
- today’s configured profit target / loss limit
- paused / running state
- Telegram update offset
- pending confirmations
- last heartbeat timestamp

## Launchd

A launchd template is included here:

`ops/launchd/com.bala.mtm-guard.bala.plist`

Before loading it:
- replace the `__...__` placeholders
- point `__PYTHON_BIN__` to the repo `.venv` python
- point `__MTM_GUARD_SCRIPT__` to `apps/risk/mtm_guard.py`
- point `__REPO_ROOT__` to the repo root
- set log paths deliberately
