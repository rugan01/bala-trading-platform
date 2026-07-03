import unittest
from datetime import date

from trading_platform.risk.daily_plan import (
    build_planning_hints,
    normalize_accounts,
    symbol_matches_expiry_underlying,
)


class DailyRiskPlanTests(unittest.TestCase):
    def test_thursday_enables_sensex_pilot_and_bans_commodities(self):
        hints = build_planning_hints(date(2026, 7, 2), weekly_payload={})
        self.assertTrue(hints["expiry_pilot"]["enabled"])
        self.assertEqual(hints["expiry_pilot"]["underlying"], "SENSEX")
        self.assertFalse(hints["commodity_allowed"])
        self.assertIn("COM", hints["banned_segments"])

    def test_weekend_is_planning_only(self):
        hints = build_planning_hints(date(2026, 7, 4), weekly_payload={})
        self.assertEqual(hints["tier1_segment"], "NO_TRADE")
        self.assertFalse(hints["expiry_pilot"]["enabled"])
        self.assertFalse(hints["commodity_allowed"])

    def test_sensex_alias_matching_does_not_match_other_indices(self):
        self.assertTrue(symbol_matches_expiry_underlying("SENSEX2641678500CE", "SENSEX"))
        self.assertTrue(symbol_matches_expiry_underlying("BSX26JULFUT", "SENSEX"))
        self.assertFalse(symbol_matches_expiry_underlying("NIFTY26JUL25000CE", "SENSEX"))

    def test_rejects_unknown_accounts(self):
        with self.assertRaises(ValueError):
            normalize_accounts(["BALA", "UNKNOWN"])


if __name__ == "__main__":
    unittest.main()
