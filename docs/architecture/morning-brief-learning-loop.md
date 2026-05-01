# Morning Brief Learning Loop

**Created**: April 30, 2026

## Why this matters

The morning brief already helps frame the market each day, but right now it does not systematically remember what it predicted, whether those predictions worked, or how confidence should change over time.

This learning loop closes that gap. Every morning or intraday brief run should become a structured observation that the platform can score end-of-day and summarize the next morning.

## Goal

Turn the morning brief into a measurable decision-support system that:
- stores every run
- stores every bullish / bearish / neutral recommendation in structured form
- evaluates what worked and what failed by horizon
- adjusts future confidence using observed hit rate and calibration
- explains the recent learning in plain English in the next brief

## Live-market applicability

Yes, the brief can and should be usable during live market hours, but only if we are honest about what is truly live-aware today.

### Current state
- `Tools/morning_brief.py` already uses live Upstox spot data inside the Nifty analysis section.
- The F&O scanner still depends on Yahoo-style data through `fetch_yahoo_data`, which is suitable for regime scanning but not yet a clean intraday live decision engine.
- MCX and index option analysis already have stronger live-data components elsewhere in the codebase, but they are not yet normalized into one structured archive.

### Immediate interpretation
- Use the learning loop for both pre-market and intraday brief runs.
- Mark each run with a `market_phase` such as `premarket`, `intraday`, or `postclose`.
- Store the exact market snapshot and confidence at that moment.
- Evaluate outcomes against the correct horizon later.

### Fastest path to value
- The F&O scanner is the easiest first slice because `Tools/fno_scanner.py` already has a JSON-capable report shape with ranked bullish and bearish stocks.
- So the first production learning loop should archive those stock recommendations exactly as generated, instead of rebuilding stock ranking logic somewhere else.
- After that, extend the same archive pattern to Nifty day-type / range-vs-trend predictions, then MCX intraday recommendations.

## Recommendation on modeling

Do **not** start with reinforcement learning.

Reinforcement learning sounds attractive, but it is the wrong first tool here because:
- the system does not yet have a clean state/action/reward archive
- your sample size will be small at first
- market regimes shift, so naive RL will overfit quickly
- it would make the brief harder to interpret and trust

### Best first approach
Start with an archive-backed evaluation and calibration loop:
1. Store structured predictions.
2. Score them at end of day or end of horizon.
3. Compute hit rates and calibration by setup family and regime.
4. Use those statistics to adjust displayed confidence and ranking.
5. Add simple supervised or bandit-style weighting only after enough observations accumulate.

### Modeling progression
1. **Phase A: Rule tracking and scorecards**
   - hit rate by signal family
   - hit rate by symbol / asset class / market phase
   - average MFE / MAE
   - calibration by confidence bucket
2. **Phase B: Probability calibration**
   - isotonic regression or Platt scaling on top of raw heuristic confidence
   - output better probability estimates without changing the core strategy logic
3. **Phase C: Adaptive weighting**
   - contextual bandit or Bayesian weighting across signal families
   - for example, reduce weight on breakout-style calls when recent narrow-CPR fade setups are outperforming
4. **Phase D: RL only if justified later**
   - only after the archive, features, actions, and reward design are stable

## What must be stored

For every brief run, store:
- timestamp
- run mode: manual, scheduled, replay
- market phase: premarket, intraday, postclose
- source version / code hash if possible
- text summary
- learning summary appended to the report

For every recommendation or prediction, store:
- asset class: index, stock, commodity, sector, global
- symbol
- timeframe
- horizon: intraday, EOD, 2-day, swing-week, expiry-week
- signal family: trend, breakout, mean reversion, relative strength, option structure, volatility regime
- predicted direction
- confidence score
- setup quality score
- entry / stop / target references if applicable
- regime label
- supporting features snapshot
- human-readable recommendation text

For every evaluated outcome, store:
- realized direction
- realized return
- max favorable excursion
- max adverse excursion
- whether target or stop would have been hit
- correctness flags by direction
- outcome score

## Learning loop workflow

### 1. Morning or intraday brief run
- run the brief
- generate the normal text report
- also emit structured predictions in JSON
- store both the text and structured payload in the archive

### 2. End-of-day evaluator
- read all unresolved predictions for the relevant horizons
- fetch the close, high, low, and realized move from the correct market data source
- compute correctness and payoff-style metrics
- write `brief_outcomes` rows

### 3. Learning summarizer
- aggregate recent outcomes over windows like 5, 20, and 60 sessions
- calculate hit rate and calibration by signal family / asset class / regime
- generate a short summary such as:
  - `Relative-strength bullish calls in stocks are 68% accurate over the last 20 sessions.`
  - `High-confidence bearish intraday index calls have recently underperformed during gap-up opens.`
- store this as a learning snapshot

### 4. Next brief enhancement
- prepend a short learning section to the next report
- optionally down-rank setups that belong to recently weak families
- optionally up-rank setups with strong recent calibration

## How this fits the bigger platform

This learning loop should not live as a standalone notebook or special script. It should sit inside the new trading platform because it depends on the same core foundations:
- canonical instrument reference
- archive-backed market observations
- strategy and signal metadata
- evaluation and reporting services

That way the same archive can later support:
- backtesting
- walk-forward validation
- paper trading
- live execution monitoring
- journaling and reconciliation

## Proposed first implementation slice

### Slice 1: Archive foundation
- initialize `platform.sqlite3`
- add `brief_runs`, `brief_predictions`, `brief_outcomes`, and `brief_learning_snapshots`
- keep raw payloads in the archive as well

### Slice 2: Structured output from `morning_brief.py`
- keep the current human-readable report
- add a machine-readable JSON sidecar
- represent every recommendation explicitly instead of only as text

### Slice 3: End-of-day evaluator
- start with simple rules:
  - direction correct or incorrect
  - realized return by horizon
  - whether target or stop was touched
- compute confidence-bucket scorecards

### Slice 4: Morning learning summary
- attach a compact section to the top of the brief:
  - what worked yesterday
  - what failed yesterday
  - which signal families are currently strongest / weakest

### Slice 5: Confidence adjustment
- modify ranking and confidence with calibration statistics
- still keep the raw model score visible for transparency

## Important caution

The system should **inform** judgment, not blindly self-optimize in a hidden way.

That means:
- preserve the raw signal and the adjusted confidence side by side
- show why a setup was down-ranked or up-ranked
- never silently change strategy rules because of one or two days of noise
- require enough sample size before a learning-based adjustment materially changes ranking

## Recommended next steps

1. Build the local archive scaffold.
2. Make `morning_brief.py` emit structured prediction objects.
3. Add an end-of-day evaluator for unresolved brief predictions.
4. Add a learning summary section to the next day report.
5. Only after enough data, introduce calibration and adaptive weighting.
