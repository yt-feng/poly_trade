from pathlib import Path
import sys, unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'analysis'))
from canary_execution_plan import build_plan, on_tick, D

class TickGuard(unittest.TestCase):
    def test_zero_blocks(self):
        r = build_plan(entry_price='.5', tick_size='0', min_order_size='5', fee_rate='.07')
        self.assertFalse(r['eligible_for_execution_validation_plan'])
        self.assertIn('blocked_tick_unknown', r['block_reasons'])
    def test_negative_blocks(self):
        self.assertFalse(on_tick(D('.5'), D('-.01')))
    def test_unknown_grid_blocks(self):
        self.assertFalse(on_tick(D('.5'), D('.03')))
    def test_nan_blocks(self):
        self.assertFalse(on_tick(D('NaN'), D('.01')))
    def test_infinity_blocks(self):
        self.assertFalse(on_tick(D('.5'), D('Infinity')))
    def test_valid_unchanged(self):
        r = build_plan(entry_price='.50', tick_size='.01', min_order_size='5', fee_rate='.07')
        self.assertTrue(r['eligible_for_execution_validation_plan'])
        self.assertEqual(r['all_in_cost_usd_equivalent'], '2.58750')
        self.assertFalse(r['live_enabled'])
    def test_min6_under_cap(self):
        r = build_plan(entry_price='.5', tick_size='.01', min_order_size='6', fee_rate='.07', notional_cap_usd='6')
        self.assertTrue(r['eligible_for_execution_validation_plan'])
        self.assertEqual(r['all_in_cost_usd_equivalent'], '3.10500')
    def test_min13_over_cap(self):
        r = build_plan(entry_price='.5', tick_size='.01', min_order_size='13', fee_rate='.07', notional_cap_usd='6')
        self.assertFalse(r['eligible_for_execution_validation_plan'])
    def test_offtick_blocks(self):
        self.assertFalse(on_tick(D('.505'), D('.01')))
    def test_boundary_blocks(self):
        for p in ('0', '1', '-1'):
            self.assertFalse(on_tick(D(p), D('.01')))

if __name__ == '__main__':
    unittest.main()
