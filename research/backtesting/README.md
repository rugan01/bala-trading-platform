# Backtesting Infrastructure

**Status**: ACTIVE — MCX CPR strategy backtested April 2026

---

## Completed Backtests

### MCX CPR Multi-Variant Strategy (April 2026)

**Platform**: TradingView PineScript v6
**Period**: Nov 2024 - Apr 2026 (17 months)

| Commodity | Best Variant | Return | Status |
|-----------|--------------|--------|--------|
| SILVERMIC | V3 + Evening | +29.09% | **RECOMMENDED** |
| ZINCMINI | V3/V4 | +15-17% | Secondary |
| CRUDEOILM | — | -6.45% | Avoid |
| NATGASMINI | — | -30.05% | Avoid |
| GOLDM | — | No trades | Avoid |

**Key Finding**: HTF EMA-200 filter hurts performance — disable it.

**Files**:
- Analysis: `mcx-cpr-tradingview-backtest-apr2026.md`
- PineScript: `mcx-cpr-multi-variant-strategy.pine`

---

## Architecture (Planned)

### Layer 1: TradingView + PineScript v6
- Custom indicators for each strategy's entry/exit signals
- Strategy tester for single-instrument backtests
- Alert system for live signal generation

### Layer 2: Python Backtesting Engine
- For multi-leg options strategies (straddles, spreads, ratio spreads)
- Historical option chain data (source TBD — NSE Bhav copies, Upstox historical API)
- Strategy-level equity curve and drawdown analysis
- Monte Carlo simulation for robustness testing

### PineScript Indicators to Build
1. **Straddle Premium Monitor**: Track ATM straddle premium as % of underlying, alert when >0.5%
2. **Multi-TF Pivot Dashboard**: CPR + pivot levels across 4 timeframes
3. **Relative Strength Ranker**: RS of F&O stocks vs Nifty, RS of commodities vs each other
4. **VIX Spike Detector**: Alert on sudden VIX increase (>X% in Y minutes)
5. **SuperTrend + MA Confluence**: Multi-indicator trend confirmation

## Data Requirements
- Nifty/Sensex/BankNifty historical OHLCV (daily, intraday)
- Historical option chain data (strike prices, premiums, OI, volume)
- India VIX historical data
- MCX commodity data (Silvermic, Goldm, Zincmini, Crudeoilm, Natgasmini)
- US commodity data (COMEX, WTI, Henry Hub) for correlation

## Success Metrics per Strategy
- Win rate (target: >55% for option selling, >40% for directional)
- Average R:R realized
- Maximum drawdown
- Sharpe ratio
- Profit factor
- Recovery time from max drawdown
