# MCX Intraday Scanner

Scans your MCX watchlist for intraday trading opportunities based on price position, volume, and risk-adjusted setups.

## Watchlist

From `Projects/trading-system/strategies/mcx-commodities-intraday.md`:

| Symbol | Name | Lot Size | Tick Value | Risk Suitability |
|--------|------|----------|------------|------------------|
| SILVERMIC | Silver Micro | 1 kg | Rs.100/tick | Good (10 ticks SL) |
| CRUDEOILM | Crude Oil Mini | 10 bbl | Rs.1000/tick | Tight (1 tick SL) |
| ZINCMINI | Zinc Mini | 1 MT | Rs.5/tick | Excellent (200 ticks SL) |
| GOLDM | Gold Mini | 100 gm | Rs.10000/tick | Too large for Rs.1000 |
| NATGASMINI | Nat Gas Mini | 250 mmBtu | Rs.2500/tick | Too large for Rs.1000 |

## Usage

```bash
# Basic scan (text output)
python Tools/mcx_scanner/mcx_scanner.py

# With custom risk budget
python Tools/mcx_scanner/mcx_scanner.py --risk 2000

# JSON output (for automation)
python Tools/mcx_scanner/mcx_scanner.py --output json

# Save to file
python Tools/mcx_scanner/mcx_scanner.py --output json --save _temp/mcx_scan.json
```

## Output

The scanner provides:

1. **Instrument Specs** - Contract details, lot sizes, tick values
2. **Live Market Data** - LTP, change%, day range, volume, position in range
3. **Trade Recommendations** - Scored setups with entry, SL, targets

## Scoring Criteria (Max 13 points)

| Factor | Points | Criteria |
|--------|--------|----------|
| Volume | 0-4 | >10k=4, >5k=3, >1k=2, >100=1 |
| Position | 0-4 | <15% or >85%=4, <25% or >75%=3, etc |
| R:R Potential | 0-3 | Range/SL: >3x=3, >2x=2, >1x=1 |
| SL Practicality | 0-2 | 5-50 ticks=2, 3-100 ticks=1 |

## Risk Management

For Rs.1000 max risk per trade:

- **SILVERMIC**: 10 ticks SL = Rs.1000 (practical)
- **CRUDEOILM**: 1 tick SL = Rs.1000 (tight, whipsaw risk)
- **ZINCMINI**: 200 ticks SL = Rs.1000 (very comfortable)
- **GOLDM**: 0.1 ticks = Not suitable for Rs.1000 risk
- **NATGASMINI**: 0.4 ticks = Not suitable for Rs.1000 risk

## Requirements

- Python 3.8+
- `requests` library
- Valid Upstox access token in `.env` file:
  ```
  UPSTOX_ACCESS_TOKEN='your_token_here'
  ```

## Integration with Trading Workflow

1. **Pre-market**: Run scanner after MCX opens (9 AM)
2. **Evening session**: Run again at 5 PM for evening opportunities
3. **Journal**: Use output to document trade decisions
4. **Backtest**: Compare recommendations vs actual moves

## Files

- `mcx_scanner.py` - Main scanner script
- `README.md` - This documentation

## Future Enhancements

- [ ] Add CPR/Pivot level analysis
- [ ] Add US market correlation (COMEX, NYMEX)
- [ ] Add relative strength ranking across commodities
- [ ] Integrate with TradingView for chart screenshots
- [ ] Add Telegram/Slack alerts for high-score setups
