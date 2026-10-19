import unittest

from analysis.h328_gross_buy_pressure_paper_replay import choose_gross_pressure


class H328Tests(unittest.TestCase):
    def test_gross_buy_share_selects_side(self):
        side, share, price = choose_gross_pressure({"Up": 8.0, "Down": 2.0}, {"Up": [0.4], "Down": []})
        self.assertEqual(side, "Up")
        self.assertAlmostEqual(share, 0.8)
        self.assertEqual(price, 0.4)

    def test_share_floor_fails_closed(self):
        side, share, price = choose_gross_pressure({"Up": 6.0, "Down": 4.0}, {"Up": [0.4], "Down": [0.4]})
        self.assertIsNone(side)
        self.assertAlmostEqual(share, 0.6)
        self.assertIsNone(price)

    def test_minimum_gross_buy_notional_fails_closed(self):
        side, share, price = choose_gross_pressure({"Up": 2.0, "Down": 0.0}, {"Up": [0.4], "Down": []})
        self.assertIsNone(side)
        self.assertEqual(share, 0.0)
        self.assertIsNone(price)


if __name__ == "__main__":
    unittest.main()
