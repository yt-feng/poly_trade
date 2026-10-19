import unittest

from analysis.h327_whale_concentration_paper_replay import choose_concentrated_side


class H327Tests(unittest.TestCase):
    def test_large_single_maker_concentration_selects_side(self):
        side, concentration, price = choose_concentrated_side(
            {"Up": 8.0, "Down": -1.0},
            {"Up": {"maker-a": 8.0}, "Down": {"maker-b": -1.0}},
            {"Up": [0.4], "Down": []},
        )
        self.assertEqual(side, "Up")
        self.assertAlmostEqual(concentration, 8.0 / 9.0)
        self.assertEqual(price, 0.4)

    def test_concentration_below_floor_fails_closed(self):
        side, concentration, price = choose_concentrated_side(
            {"Up": 8.0, "Down": 1.0},
            {"Up": {"maker-a": 4.0, "maker-b": 4.0}, "Down": {"maker-c": 1.0}},
            {"Up": [0.4], "Down": []},
        )
        self.assertIsNone(side)
        self.assertAlmostEqual(concentration, 4.0 / 9.0)
        self.assertIsNone(price)

    def test_small_total_notional_fails_closed(self):
        side, concentration, price = choose_concentrated_side(
            {"Up": 2.0, "Down": 0.0},
            {"Up": {"maker-a": 2.0}, "Down": {}},
            {"Up": [0.4], "Down": []},
        )
        self.assertIsNone(side)
        self.assertEqual(concentration, 0.0)
        self.assertIsNone(price)


if __name__ == "__main__":
    unittest.main()
