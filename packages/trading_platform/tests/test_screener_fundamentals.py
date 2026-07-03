import unittest

from trading_platform.stock_selection.screener import parse_screener_html
from trading_platform.stock_selection.universe import current_fno_stock_symbols


HTML = """
<html><body>
<ul id="top-ratios">
 <li><span class="name">Current Price</span><span class="number">200</span></li>
 <li><span class="name">Stock P/E</span><span class="number">20</span></li>
 <li><span class="name">Book Value</span><span class="number">100</span></li>
 <li><span class="name">Dividend Yield</span><span class="number">1.5%</span></li>
 <li><span class="name">ROE</span><span class="number">18%</span></li>
</ul>
<section id="profit-loss"><table><thead><tr><th></th><th>Mar 2022</th><th>Mar 2023</th><th>Mar 2024</th><th>Mar 2025</th></tr></thead><tbody>
<tr><td>Sales +</td><td>100</td><td>120</td><td>150</td><td>200</td></tr>
<tr><td>Operating Profit</td><td>20</td><td>24</td><td>33</td><td>50</td></tr>
<tr><td>Interest</td><td>5</td><td>5</td><td>5</td><td>5</td></tr>
<tr><td>Net Profit +</td><td>10</td><td>12</td><td>18</td><td>30</td></tr>
</tbody></table></section>
<section id="balance-sheet"><table><thead><tr><th></th><th>Mar 2022</th><th>Mar 2023</th><th>Mar 2024</th><th>Mar 2025</th></tr></thead><tbody>
<tr><td>Equity Capital</td><td>10</td><td>10</td><td>10</td><td>10</td></tr>
<tr><td>Reserves</td><td>40</td><td>50</td><td>65</td><td>90</td></tr>
<tr><td>Borrowings +</td><td>50</td><td>48</td><td>40</td><td>30</td></tr>
<tr><td>Total Assets</td><td>200</td><td>220</td><td>250</td><td>300</td></tr>
</tbody></table></section>
<section id="cash-flow"><table><thead><tr><th></th><th>Mar 2022</th><th>Mar 2023</th><th>Mar 2024</th><th>Mar 2025</th></tr></thead><tbody>
<tr><td>Cash from Operating Activity +</td><td>12</td><td>15</td><td>24</td><td>40</td></tr>
<tr><td>Free Cash Flow</td><td>5</td><td>7</td><td>12</td><td>20</td></tr>
</tbody></table></section>
<section id="ratios"><table><thead><tr><th></th><th>Mar 2022</th><th>Mar 2023</th><th>Mar 2024</th><th>Mar 2025</th></tr></thead><tbody>
<tr><td>ROCE %</td><td>12</td><td>14</td><td>17</td><td>21</td></tr>
</tbody></table></section>
<section id="shareholding"><table><thead><tr><th></th><th>Mar 2022</th><th>Mar 2023</th><th>Mar 2024</th><th>Mar 2025</th></tr></thead><tbody>
<tr><td>Promoters +</td><td>60</td><td>60</td><td>61</td><td>62</td></tr>
<tr><td>FIIs +</td><td>10</td><td>11</td><td>12</td><td>13</td></tr>
<tr><td>DIIs +</td><td>5</td><td>6</td><td>7</td><td>8</td></tr>
</tbody></table></section>
</body></html>
"""


class ScreenerFundamentalsTest(unittest.TestCase):
    def test_parses_latest_and_point_in_time_history(self):
        latest, history = parse_screener_html("TEST", HTML)
        self.assertEqual(len(history), 4)
        self.assertAlmostEqual(latest["sales_yoy"], 33.3333, places=3)
        self.assertAlmostEqual(latest["sales_cagr_3y"], 25.9921, places=3)
        self.assertEqual(latest["roce"], 21)
        self.assertEqual(latest["pe"], 20)
        self.assertEqual(latest["pb"], 2)
        self.assertAlmostEqual(latest["debt_to_equity"], 0.3)
        self.assertEqual(history[0]["as_of_date"], "2022-03-31")
        self.assertNotIn("pe", history[0])
        self.assertEqual(history[0]["promoter_holding"], 60)

    def test_current_fno_stock_symbols_filters_equity_underlyings(self):
        instruments = [
            {
                "segment": "NSE_FO",
                "underlying_type": "EQUITY",
                "underlying_symbol": "RELIANCE",
            },
            {
                "segment": "NSE_FO",
                "underlying_type": "INDEX",
                "underlying_symbol": "NIFTY",
            },
            {
                "segment": "NSE_EQ",
                "underlying_type": "EQUITY",
                "underlying_symbol": "TCS",
            },
        ]

        self.assertEqual(current_fno_stock_symbols(instruments), ["RELIANCE"])


if __name__ == "__main__":
    unittest.main()
