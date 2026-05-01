# Walk-Forward Engine

Paper-trading and replay framework for validating strategies before live execution.

Current live paper profile focus:
- `silvermic_v3_default`
- `silvermic_breakout_research`

The engine remains **paper-only**. It does not place live broker orders.

## Main entry points

- `main.py`
- `replay.py`
- `replay_batch.py`
- `runner_profiles.py`
- `strategy_registry.py`
- `position_plans.py`

## Quick start

From the repo root:

```bash
cd /path/to/bala-trading-platform
./.venv/bin/python -m pip install -r apps/walk-forward/requirements.txt

./.venv/bin/python apps/walk-forward/main.py --list-profiles
./.venv/bin/python apps/walk-forward/main.py --profile-id silvermic_v3_default --dry-run
./.venv/bin/python apps/walk-forward/main.py --profile-id silvermic_v3_default
```

Run multiple profiles:

```bash
./.venv/bin/python apps/walk-forward/main.py \
  --profile-id silvermic_v3_default \
  --profile-id silvermic_breakout_research \
  --dry-run
```

## Replay

```bash
./.venv/bin/python apps/walk-forward/replay.py --self-test

./.venv/bin/python apps/walk-forward/replay.py \
  --csv /path/to/candles.csv \
  --session-date YYYY-MM-DD \
  --run-id test_run
```

Batch replay:

```bash
./.venv/bin/python apps/walk-forward/replay_batch.py --list-strategies
./.venv/bin/python apps/walk-forward/replay_batch.py --list-position-plans
```

## Configuration

Defaults come from the repo-local `.env`.

Important variables:
- `UPSTOX_ACCESS_TOKEN`
- `NOTION_API_KEY`
- `NOTION_WF_DB_ID`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- optional `WFV_PROFILE_ID`
- optional manual instrument overrides like `UPSTOX_SILVERMIC_KEY`

## Outputs

Logs:
- `~/Library/Logs/walk_forward/`

Replay artifacts:
- `apps/walk-forward/output/replay/` when using the original app-local defaults
- or the explicitly supplied output directory

## Related docs

- `LLM_HANDOVER.md`
- `ROADMAP.md`
- `MILESTONE_1_DESIGN.md`

These files contain the deeper research and migration context. Some sections still reflect the earlier local workspace history, so use this README and the repo runbook for the current commands.
