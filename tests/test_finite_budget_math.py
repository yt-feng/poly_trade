"""Synthetic mathematical identities, not empirical trading evidence."""
import copy
import json
import math
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from finite_budget_math import (number, binary_states, evaluate_stake,
    binary_thresholds, minimum_bankroll_boundary, compare_supplied_stakes)


class FiniteBudgetTests(unittest.TestCase):
    def setUp(self):
        self.x = binary_states(.26, 3, 1, 30)
        self.args = dict(equity=10, available_cash=10, minimum_stake=1,
                         all_in_cap=3, legal_stakes=[1,2,3], states=self.x)
    def test_cash_positive_log_negative(self):
        r=evaluate_stake(10,1,self.x)
        self.assertAlmostEqual(r['expected_net_cash_pnl'],.04)
        self.assertLess(r['expected_log_change'],0)
    def test_zero_is_explicit_alternative(self):
        r=compare_supplied_stakes(**self.args)
        self.assertEqual(r['argmax_stake_in_supplied_scenarios'],0)
    def test_more_capital_same_minimum_can_change_log_sign(self):
        self.assertGreater(evaluate_stake(100,1,self.x)['expected_log_change'],0)
    def test_twenty_still_negative(self):
        self.assertLess(evaluate_stake(20,1,self.x)['expected_log_change'],0)
    def test_fifty_positive(self):
        self.assertGreater(evaluate_stake(50,1,self.x)['expected_log_change'],0)
    def test_continuous_fraction_not_rounded_up(self):
        t=binary_thresholds(.26,3,1,.1)
        self.assertAlmostEqual(t['continuous_kelly_fraction_supplied_binary_model'],1/75)
        self.assertEqual(compare_supplied_stakes(**self.args)['argmax_stake_in_supplied_scenarios'],0)
    def test_log_probability_threshold_above_cash_threshold(self):
        t=binary_thresholds(.26,3,1,.1)
        self.assertGreater(t['log_break_even_win_probability_at_fraction'],t['cash_break_even_win_probability'])
    def test_probability_threshold_identity(self):
        p=binary_thresholds(.26,3,1,.1)['log_break_even_win_probability_at_fraction']
        self.assertAlmostEqual(evaluate_stake(10,1,binary_states(p,3,1))['expected_log_change'],0,places=14)
    def test_bankroll_boundary(self):
        r=minimum_bankroll_boundary(1,.26,3,1)
        w=r['bankroll_boundary']
        self.assertGreater(w,20);self.assertLess(w,50)
        self.assertLess(evaluate_stake(.999*w,1,self.x)['expected_log_change'],0)
        self.assertGreater(evaluate_stake(1.001*w,1,self.x)['expected_log_change'],0)
    def test_bad_edge_not_fixed_by_capital(self):
        self.assertIsNone(minimum_bankroll_boundary(1,.2,3,1)['bankroll_boundary'])
    def test_fair_edge_not_positive_growth(self):
        self.assertIsNone(minimum_bankroll_boundary(1,.25,3,1)['bankroll_boundary'])
    def test_certain_gain(self):
        r=minimum_bankroll_boundary(1,1,3,1)
        self.assertEqual(r['bankroll_boundary'],1)
        self.assertGreater(evaluate_stake(1,1,binary_states(1,3,1))['expected_log_change'],0)
    def test_full_stake_possible_ruin(self):
        r=evaluate_stake(1,1,self.x)
        self.assertIsNone(r['expected_log_change'])
        self.assertFalse(r['positive_log_expectation'])
        json.dumps(r,allow_nan=False)
    def test_ruin_threshold(self):
        self.assertEqual(binary_thresholds(.5,3,1,1)['log_break_even_win_probability_at_fraction'],1)
    def test_zero_stake(self):
        self.assertEqual(evaluate_stake(10,0,self.x)['expected_log_change'],0)
    def test_duration_reduces_rate_not_pnl(self):
        a=evaluate_stake(100,1,self.x);b=evaluate_stake(100,1,binary_states(.26,3,1,300))
        self.assertAlmostEqual(a['expected_net_cash_pnl'],b['expected_net_cash_pnl'])
        self.assertAlmostEqual(a['log_change_per_expected_second'],10*b['log_change_per_expected_second'])
    def test_rejection_duration_not_dropped(self):
        x=[dict(probability=.5,net_return=0,cash_cycle_seconds=90),dict(probability=.5,net_return=.1,cash_cycle_seconds=10)]
        self.assertEqual(evaluate_stake(10,1,x)['expected_cash_cycle_seconds'],50)
    def test_unknown_exit_return_rejected(self):
        x=copy.deepcopy(self.x);x[0]['net_return']=None
        self.assertRaises(ValueError,evaluate_stake,10,1,x)
    def test_unknown_cash_time_rejected(self):
        x=copy.deepcopy(self.x);x[0]['cash_cycle_seconds']=None
        self.assertRaises(ValueError,evaluate_stake,10,1,x)
    def test_probability_sum(self):
        x=copy.deepcopy(self.x);x[0]['probability']=.9
        self.assertRaises(ValueError,evaluate_stake,10,1,x)
    def test_leverage_rejected(self):self.assertRaises(ValueError,evaluate_stake,10,11,self.x)
    def test_empty_distribution(self):self.assertRaises(ValueError,evaluate_stake,10,1,[])
    def test_boolean_rejected(self):self.assertRaises(ValueError,number,True)
    def test_nan_rejected(self):self.assertRaises(ValueError,number,float('nan'))
    def test_infinity_rejected(self):self.assertRaises(ValueError,number,float('inf'))
    def test_negative_equity(self):self.assertRaises(ValueError,evaluate_stake,-1,0,self.x)
    def test_zero_duration(self):self.assertRaises(ValueError,binary_states,.5,1,1,0)
    def test_loss_beyond_stake_rejected(self):self.assertRaises(ValueError,binary_states,.5,1,1.1)
    def test_frozen_cash_not_available(self):
        a=dict(self.args,available_cash=.5)
        r=compare_supplied_stakes(**a)
        self.assertIn('MINIMUM_EXCEEDS_AVAILABLE_CASH',r['reasons'])
        self.assertEqual(r['argmax_stake_in_supplied_scenarios'],0)
    def test_risk_cap_stays_binding(self):
        r=compare_supplied_stakes(**dict(self.args,all_in_cap=.5))
        self.assertIn('MINIMUM_EXCEEDS_RISK_CAP',r['reasons'])
    def test_legal_minimum_not_invented(self):
        self.assertRaises(ValueError,compare_supplied_stakes,**dict(self.args,legal_stakes=[.2]))
    def test_duplicate_lot(self):
        self.assertRaises(ValueError,compare_supplied_stakes,**dict(self.args,legal_stakes=[1,1]))
    def test_cash_cannot_exceed_equity(self):
        self.assertRaises(ValueError,compare_supplied_stakes,**dict(self.args,available_cash=11))
    def test_inputs_unmodified(self):
        before=copy.deepcopy(self.args);compare_supplied_stakes(**self.args);self.assertEqual(before,self.args)
    def test_no_approval_or_authenticated_facts(self):
        r=compare_supplied_stakes(**dict(self.args,equity=100,available_cash=100))
        for key in ('exchange_legality_verified','probabilities_verified','cash_receipt_verified','human_canary_approved','orders_enabled'):
            self.assertFalse(r[key])
    def test_state_order_invariance(self):
        self.assertEqual(evaluate_stake(10,1,self.x),evaluate_stake(10,1,list(reversed(self.x))))
    def test_scaled_cash_same_growth(self):
        a=evaluate_stake(10,1,self.x);b=evaluate_stake(100,10,self.x)
        self.assertAlmostEqual(a['expected_log_change'],b['expected_log_change'])
    def test_optimal_fraction_local_maximum(self):
        f=binary_thresholds(.26,3,1,.1)['continuous_kelly_fraction_supplied_binary_model']
        v=evaluate_stake(1,f,self.x)['expected_log_change']
        self.assertGreater(v,evaluate_stake(1,.9*f,self.x)['expected_log_change'])
        self.assertGreater(v,evaluate_stake(1,1.1*f,self.x)['expected_log_change'])
    def test_partial_loss_supported(self):
        t=binary_thresholds(.4,.8,.2,.1)
        self.assertAlmostEqual(t['cash_break_even_win_probability'],.2)
    def test_empty_lots_means_only_cash(self):
        self.assertEqual(compare_supplied_stakes(**dict(self.args,legal_stakes=[]))['argmax_stake_in_supplied_scenarios'],0)

if __name__=='__main__':unittest.main()
