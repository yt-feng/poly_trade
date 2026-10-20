import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis.h423_orderfilled_book_tape_replay import candidate_from_history, evaluate_chain
import pandas as pd

class H423ContractTests(unittest.TestCase):
    def test_candidate_uses_only_pre_entry_history_and_fixed_gate(self):
        market = pd.Series({'market_slug':'m','start_time':pd.Timestamp('2026-08-08T00:00:00Z'),'end_time':pd.Timestamp('2026-08-08T00:05:00Z'),'winning_outcome':'yes'})
        history = pd.DataFrame([
            {'timestamp':pd.Timestamp('2026-08-08T00:01:00Z'),'outcome':'yes','taker_side':'BUY','price':0.40,'usd_amount':30.0,'token_amount':75.0},
            {'timestamp':pd.Timestamp('2026-08-08T00:02:00Z'),'outcome':'yes','taker_side':'BUY','price':0.41,'usd_amount':30.0,'token_amount':73.0},
            {'timestamp':pd.Timestamp('2026-08-08T00:03:30Z'),'outcome':'yes','taker_side':'BUY','price':0.40,'usd_amount':30.0,'token_amount':75.0},
            {'timestamp':pd.Timestamp('2026-08-08T00:04:30Z'),'outcome':'yes','taker_side':'BUY','price':0.99,'usd_amount':999.0,'token_amount':1000.0},
        ])
        event = candidate_from_history(market, history[history.timestamp < pd.Timestamp('2026-08-08T00:04:00Z')])
        self.assertIsNotNone(event)
        self.assertEqual(event['signal'], 'yes')
        self.assertAlmostEqual(event['entry_price'], 0.41)
        self.assertTrue(event['correct'])

    def test_chain_requires_positive_prior_release_and_no_reset(self):
        base = {'entry_ts': 1_000, 'cash_available_ts': 901_000, 'entry_cost': 4.95, 'fee': 0.003465, 'status':'paper'}
        first = dict(base, slug='a', payout=5.0)
        loss = dict(base, slug='b', entry_ts=902_000, cash_available_ts=1_802_000, payout=0.0)
        after_loss = dict(base, slug='c', entry_ts=1_803_000, cash_available_ts=2_703_000, payout=5.0)
        result = evaluate_chain([first, loss, after_loss])
        self.assertEqual(result['fills'], 2)
        self.assertEqual(result['failure_reason'], 'prior_cash_release_required')

if __name__ == '__main__':
    unittest.main()
