import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

JOURNAL_APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(JOURNAL_APP))

from trade_journaling import ParsedInstrument, TradeProcessor, UpstoxClient  # noqa: E402


class TradeJournalingParsingTests(unittest.TestCase):
    def setUp(self):
        self.client = UpstoxClient.__new__(UpstoxClient)

    def test_parses_current_index_day_first_symbol(self):
        parsed = self.client.parse_trading_symbol("NIFTY26MAY23850PE")
        self.assertEqual(parsed.base_symbol, "NIFTY")
        self.assertEqual(parsed.expiry_date, date(datetime.now().year, 5, 26))
        self.assertEqual(parsed.strike, 23850.0)
        self.assertEqual(parsed.instrument_type, "PE")

    def test_parses_compact_sensex_weekly_symbol(self):
        parsed = self.client.parse_trading_symbol("SENSEX2641678500CE")
        self.assertEqual(parsed.expiry_date, date(2026, 4, 16))
        self.assertEqual(parsed.strike, 78500.0)

    def test_parses_historical_mcx_day_first_symbol(self):
        parsed = self.client.parse_trading_symbol("CRUDEOILM16APR268700PE")
        self.assertEqual(parsed.base_symbol, "CRUDEOILM")
        self.assertEqual(parsed.expiry_date, date(2026, 4, 16))
        self.assertEqual(parsed.strike, 8700.0)

    def test_instrument_master_expiry_and_strike_take_precedence(self):
        processor = TradeProcessor.__new__(TradeProcessor)
        parsed = ParsedInstrument("NIFTY", date(2026, 4, 30), "CE", 24000.0)
        exact = datetime(2026, 4, 28, tzinfo=timezone.utc)
        details = {"expiry": int(exact.timestamp() * 1000), "strike_price": "24100"}
        self.assertEqual(processor._normalized_expiry_date(parsed, details), date(2026, 4, 28))
        self.assertEqual(processor._normalized_option_strike(parsed, details), 24100.0)


    def test_mcx_strike_beginning_with_year_digits_is_not_truncated(self):
        """SILVERM26AUG265000CE must parse as strike 265000, not 5000.

        The day-first regex also matches this symbol, reading "26" as a year and
        leaving "5000" as the strike. Both year readings are plausible (26), so
        the existing year-distance check cannot separate them. Journalled as
        strike 5000 on 2026-08-10 before this was fixed.
        """
        parsed = self.client.parse_trading_symbol("SILVERM26AUG265000CE")
        self.assertEqual(parsed.base_symbol, "SILVERM")
        self.assertEqual(parsed.strike, 265000.0)
        self.assertEqual(parsed.instrument_type, "CE")

    def test_mcx_strike_not_beginning_with_year_digits_still_parses(self):
        parsed = self.client.parse_trading_symbol("SILVERM26AUG215000PE")
        self.assertEqual(parsed.strike, 215000.0)
        self.assertEqual(parsed.instrument_type, "PE")

    def test_day_first_wins_when_monthly_year_is_not_current(self):
        """LTM25AUG264800CE is day 25 / AUG / year 26 / strike 4800.

        The monthly reading (year 25, strike 264800) also matches, and its strike
        begins with the day-first year digits, so the same-year tie-break would
        fire on a tolerance. It must not: the monthly year here is 25, not the
        current year, so the day-first reading is correct. Guards the inversion
        introduced when the tie-break used +/-1 slack instead of exact equality.
        """
        parsed = self.client.parse_trading_symbol("LTM25AUG264800CE")
        self.assertEqual(parsed.base_symbol, "LTM")
        self.assertEqual(parsed.strike, 4800.0)
        self.assertEqual(parsed.expiry_date, date(2026, 8, 25))

    def test_compact_monthly_equity_option_symbol(self):
        parsed = self.client.parse_trading_symbol("LTM26AUG4800CE")
        self.assertEqual(parsed.strike, 4800.0)
        self.assertEqual(parsed.instrument_type, "CE")

    def test_historical_day_first_mcx_symbol_does_not_regress(self):
        """The day-first form must still win where the monthly year is implausible."""
        parsed = self.client.parse_trading_symbol("CRUDEOILM16APR268700PE")
        self.assertEqual(parsed.base_symbol, "CRUDEOILM")
        self.assertEqual(parsed.strike, 8700.0)
        self.assertEqual(parsed.expiry_date, date(2026, 4, 16))



class InstrumentMasterMatchTests(unittest.TestCase):
    """Derivatives must resolve against the master's structured fields.

    The master writes derivatives spaced out ("LTM 4800 CE 25 AUG 26"), which
    parse_trading_symbol cannot read, so matching by re-parsing that display
    string never succeeded. Every historical NSE/BSE option then fell through to
    the loose match, which deliberately withholds instrument_key for
    derivatives, and fee calculation failed hard on any past-date run.
    """

    def setUp(self):
        self.client = UpstoxClient.__new__(UpstoxClient)

    def row(self, **over):
        base = {
            "trading_symbol": "LTM 4800 CE 25 AUG 26",
            "instrument_key": "NSE_FO|119520",
            "asset_symbol": "LTM",
            "strike_price": 4800.0,
            "instrument_type": "CE",
            "expiry": 1787682599000,
            "lot_size": 150,
        }
        base.update(over)
        return base

    def parsed(self):
        return self.client.parse_trading_symbol("LTM25AUG264800CE")

    def test_expiry_epoch_converts_in_ist(self):
        self.assertEqual(self.client._master_expiry_date(1787682599000), date(2026, 8, 25))

    def test_bad_expiry_values_do_not_raise(self):
        for bad in (None, "", "not-a-number"):
            self.assertIsNone(self.client._master_expiry_date(bad))

    def test_matches_the_right_contract(self):
        self.assertTrue(self.client._master_row_matches(self.row(), self.parsed()))

    def test_rejects_wrong_strike_expiry_type_and_underlying(self):
        p = self.parsed()
        self.assertFalse(self.client._master_row_matches(self.row(strike_price=4900.0), p))
        self.assertFalse(self.client._master_row_matches(self.row(expiry=1793125799000), p))
        self.assertFalse(self.client._master_row_matches(self.row(instrument_type="PE"), p))
        self.assertFalse(self.client._master_row_matches(self.row(asset_symbol="LT"), p))

    def test_falls_back_to_underlying_symbol_when_asset_symbol_absent(self):
        row = self.row()
        del row["asset_symbol"]
        row["underlying_symbol"] = "LTM"
        self.assertTrue(self.client._master_row_matches(row, self.parsed()))

if __name__ == "__main__":
    unittest.main()
