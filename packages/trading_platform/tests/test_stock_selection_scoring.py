import unittest

from trading_platform.stock_selection.scoring import (
    build_snapshot,
    build_stock_scorecards,
    score_fundamentals,
)


class StockSelectionScoringTests(unittest.TestCase):
    def setUp(self):
        self.benchmark = [100 + index * 0.2 for index in range(180)]

    def test_stronger_stock_ranks_above_weaker_stock(self):
        strong = build_snapshot(
            "STRONG",
            "Industrials",
            [100 + index * 0.8 for index in range(180)],
            self.benchmark,
            1_000_000,
        )
        weak = build_snapshot(
            "WEAK",
            "Industrials",
            [180 - index * 0.2 for index in range(180)],
            self.benchmark,
            500_000,
        )
        cards, _ = build_stock_scorecards([strong, weak])
        self.assertEqual(cards[0].symbol, "STRONG")
        self.assertGreater(cards[0].momentum_score, cards[1].momentum_score)

    def test_missing_fundamentals_requires_research(self):
        snapshot = build_snapshot(
            "TEST",
            "Industrials",
            [100 + index * 0.5 for index in range(180)],
            self.benchmark,
            1_000_000,
        )
        cards, _ = build_stock_scorecards([snapshot])
        self.assertEqual(cards[0].status, "RESEARCH_REQUIRED")

    def test_direct_fundamental_scores_and_red_flag(self):
        score = score_fundamentals(
            {
                "growth_score": "80",
                "profitability_score": "75",
                "efficiency_score": "70",
                "solvency_score": "65",
                "quality_score": "90",
                "valuation_score": "60",
                "promoter_pledge": "30",
            }
        )
        self.assertAlmostEqual(score.overall, 73.3, places=1)
        self.assertIn("Promoter pledge above 20%", score.red_flags)


if __name__ == "__main__":
    unittest.main()
