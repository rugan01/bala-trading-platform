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

    def test_historical_day_first_mcx_symbol_does_not_regress(self):
        """The day-first form must still win where the monthly year is implausible."""
        parsed = self.client.parse_trading_symbol("CRUDEOILM16APR268700PE")
        self.assertEqual(parsed.base_symbol, "CRUDEOILM")
        self.assertEqual(parsed.strike, 8700.0)
        self.assertEqual(parsed.expiry_date, date(2026, 4, 16))


if __name__ == "__main__":
    unittest.main()
