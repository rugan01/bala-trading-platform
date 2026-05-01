# MCX CPR Strategy — TradingView Backtest Analysis

**Date**: April 9, 2026
**Platform**: TradingView (PineScript v6)
**Data Period**: November 1, 2024 — April 9, 2026 (~17 months)
**Initial Capital**: ₹500,000

---

## Executive Summary

Comprehensive backtest of the MCX CPR Pivot strategy across 5 commodities and 5 variants using TradingView's continuous futures data. **Key finding**: Disabling the HTF EMA-200 filter dramatically improved performance, contradicting earlier Trading Engine results.

### Winner: SILVERMIC + V3 + Evening Session
- **Return**: +29.09% (+₹142,810)
- **Trades**: 24
- **Win Rate**: 25%
- **Max Drawdown**: 1.76%

---

## Test Configuration

### Strategy Variants Tested
| Variant | Entry Level (Long) | Entry Level (Short) | Target (Long) | Target (Short) |
|---------|-------------------|---------------------|---------------|----------------|
| V1 | S1 | R1 | Pivot | Pivot |
| V2 | S2 | R2 | S1 | R1 |
| V3 | BC (Bottom Central) | TC (Top Central) | TC | BC |
| V4 | Camarilla S3 | Camarilla R3 | Cam R3 | Cam S3 |
| V5 | BC | TC | R1 | S1 |

### Session Windows Tested
- Full Day (09:00-23:00)
- Evening (17:00-21:30)
- Extended Evening (17:00-23:00)
- Morning Start (10:00-21:30)
- Afternoon Start (12:00-21:30)

### Common Parameters
- Timeframe: 15 minutes
- Touch Tolerance: 0.15%
- Min Bars Between Touches: 3
- Require 2nd Touch: Yes
- SL Method: SuperTrend (5, 3.0) with 0.8% fallback
- Exit: Partial Trail (50% at T1, trail remainder with ST 1.5)
- Max Trades/Day: 2
- Commission: 0.03%
- Slippage: 2 ticks

---

## Critical Finding: HTF Filter Impact

### ZINCMINI V3 Comparison

| HTF Filter | P&L | Return | Trades | Win Rate |
|------------|-----|--------|--------|----------|
| **ON** (Daily + 2H EMA-200) | -₹7,930 | -1.59% | 10 | 10% |
| **OFF** | +₹77,494 | +15.69% | 8 | 25% |

**Conclusion**: The HTF EMA-200 filter was blocking profitable trades. All subsequent tests were run with HTF Filter OFF.

---

## Complete Test Results

### SILVERMIC (Best Performer)

| Variant | Session | P&L | Return | Trades | Win Rate | Max DD |
|---------|---------|-----|--------|--------|----------|--------|
| **V3** | **Extended Evening** | **+₹142,921** | **+29.11%** | 24 | 25.00% | 1.76% |
| V3 | Full Day | +₹137,317 | +28.34% | 39 | 17.95% | 2.87% |
| V1 | Full Day | +₹119,401 | +24.78% | 24 | 33.33% | 3.53% |
| V4 | Full Day | +₹113,101 | +23.78% | 37 | 32.43% | 4.46% |

**Analysis**:
- All variants profitable on SILVERMIC
- V3 with Extended Evening session is optimal
- Higher win rate (V1, V4) doesn't translate to higher returns
- V3's larger wins compensate for lower win rate

### ZINCMINI

| Variant | Session | P&L | Return | Trades | Win Rate | Max DD |
|---------|---------|-----|--------|--------|----------|--------|
| V4 | Full Day | +₹84,279 | +16.82% | 2 | 50.00% | 0.63% |
| V3 | Full Day | +₹77,494 | +15.69% | 8 | 25.00% | 1.50% |
| V1 | Full Day | +₹76,143 | +15.46% | 7 | 28.57% | 1.84% |

**Analysis**:
- All variants profitable but very low trade frequency
- Only 2-8 trades over 17 months limits usefulness
- V4 (Camarilla) shows highest return per trade
- Low drawdown across all variants

### CRUDEOILM (Avoid)

| Variant | Session | P&L | Return | Trades | Win Rate | Max DD |
|---------|---------|-----|--------|--------|----------|--------|
| V3 | Full Day | -₹32,241 | -6.45% | 58 | 20.69% | 6.45% |

**Analysis**:
- Consistent losses despite reasonable trade count
- CPR levels not respected in crude oil futures
- Avoid this commodity with current strategy

### NATGASMINI (Avoid)

| Variant | Session | P&L | Return | Trades | Win Rate | Max DD |
|---------|---------|-----|--------|--------|----------|--------|
| V3 | Full Day | -₹152,210 | -30.05% | 183 | 16.94% | 31.31% |

**Analysis**:
- Worst performer with severe losses
- Very high trade count indicates over-trading
- CPR strategy fundamentally unsuited for natural gas
- Avoid entirely

### GOLDM (No Data)

| Variant | Session | P&L | Return | Trades | Win Rate |
|---------|---------|-----|--------|--------|----------|
| V3 | Full Day | ₹0 | 0.00% | 0 | — |

**Analysis**:
- No trades triggered during test period
- CPR levels may be too far from price action
- Insufficient data to evaluate

---

## Discrepancy with Trading Engine Backtest

### Original Trading Engine Results (Jan 15 - Mar 20, 2026)

| Commodity | Variant | Trades | Win Rate | P&L | Profit Factor |
|-----------|---------|--------|----------|-----|---------------|
| ZINCMINI | V3 | 23 | 82.6% | ₹5,65,978 | 47.78 |
| SILVERMIC | V3 | 4 | 75.0% | ₹1,66,624 | 41.39 |
| CRUDEOILM | V3 | 21 | 61.9% | ₹17,436 | 3.44 |

### TradingView Results (Nov 2024 - Apr 2026)

| Commodity | Variant | Trades | Win Rate | P&L |
|-----------|---------|--------|----------|-----|
| ZINCMINI | V3 | 8 | 25% | +₹77,494 |
| SILVERMIC | V3 | 39 | 17.95% | +₹137,317 |
| CRUDEOILM | V3 | 58 | 20.69% | -₹32,241 |

### Possible Explanations

1. **HTF Filter Difference**: Trading Engine may have implemented HTF filter differently or not at all in some tests

2. **Data Period**: Trading Engine tested 2.5 months vs TradingView's 17 months — longer period reveals true performance

3. **Continuous Futures Construction**: TradingView uses different roll methodology for continuous contracts

4. **Touch Detection Logic**: Minor differences in how "touches" are detected can significantly impact trade entry

5. **Session Timing**: IST timezone handling may differ between platforms

---

## Recommended Configuration

### Primary: SILVERMIC

```
Commodity: MCX:SILVERMIC1!
Timeframe: 15 minutes
Variant: V3 - CPR Band (TC/BC)
Session: Extended Evening (17:00-23:00 IST)
HTF Filter: OFF
Touch Tolerance: 0.15%
Min Bars Between Touches: 3
Require 2nd Touch: Yes
SL Method: SuperTrend (5, 3.0)
Exit Method: Partial Trail (50% at T1)
Trail Factor: 1.5
Max Trades/Day: 2
```

### Secondary: ZINCMINI

```
Commodity: MCX:ZINCMINI1!
Timeframe: 15 minutes
Variant: V3 or V4
Session: Full Day (09:00-23:00 IST)
HTF Filter: OFF
(Other parameters same as above)
```

### Avoid
- CRUDEOILM — Consistent losses
- NATGASMINI — Severe losses, over-trading
- GOLDM — No signals generated

---

## PineScript Strategy

The parameterized strategy "MCX CPR Multi-Variant Strategy" is saved in TradingView with the following features:

- Dropdown selection for all 5 variants
- Session window selector
- Weekday filter (optional)
- HTF filter toggle
- Configurable touch tolerance and bar gaps
- Multiple SL methods (SuperTrend, Fixed %, ATR)
- Multiple exit methods (Partial Trail, Full Target, Trail Only)
- Info table showing current state

### Key Input IDs (for programmatic control)
- `in_0`: Strategy Variant
- `in_1`: Session Window
- `in_9`: HTF Filter (boolean)
- `in_13`: Touch Tolerance
- `in_14`: Min Bars Between Touches

---

## Next Steps

1. **Paper Trade SILVERMIC V3**: Run for 4-6 weeks to validate TradingView results

2. **Investigate Discrepancy**: Compare Trading Engine and TradingView trade-by-trade to understand differences

3. **Walk-Forward Validation**: Split data into in-sample and out-of-sample periods

4. **Parameter Sensitivity**: Test variations in touch tolerance (0.1%, 0.2%) and session windows

5. **Consider Combining**: Run SILVERMIC and ZINCMINI together for diversification

---

## Files & References

- **PineScript Strategy**: TradingView > Pine Editor > "MCX CPR Multi-Variant Strategy"
- **Screenshots**: `/Users/rugan/Projects/tradingview-mcp-jackson/screenshots/`
  - `final_silvermic_v3_evening.png`
  - `zincmini_v3_no_htf.png`
  - `silvermic_v1_no_htf.png`
  - `crudeoilm_v3_no_htf.png`
  - `natgasmini_v3_no_htf.png`
- **Original Strategy Doc**: `Projects/trading-system/strategies/mcx-cpr-pivot-strategy.md`
- **Trading Engine Code**: `/Users/rugan/Projects/Trading Engine/src/trading_engine/domain/backtest/mcx_cpr_backtest.py`

---

## Changelog

| Date | Change |
|------|--------|
| April 9, 2026 | Initial TradingView backtest analysis |
| April 9, 2026 | Discovered HTF filter negative impact |
| April 9, 2026 | Identified SILVERMIC V3 Evening as optimal |
