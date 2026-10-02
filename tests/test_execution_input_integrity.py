"""Synthetic malformed-batch and cumulative cash-closure contracts.

Pure read-only diagnostics: no network, SDK, account, order or cash mutation.
"""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'analysis'))
from execution_reason_codes import audit_episode_identity, classify, summarize


def state(**updates):
    row = dict(
        execution_identity=dict(account_scope='testnet:synthetic-account',
            collateral_asset='synthetic-asset', condition_id='synthetic-condition',
            episode_id='synthetic-episode', token_id='synthetic-token'),
        observation_ms=1000, submission_reference='synthetic-order',
        signal_qualified=True, planned_shares='5', min_order_size='5',
        requested_shares='5', confirmed_filled_shares='5',
        remaining_position_shares='0', entry_order_terminal=True,
        order_terminal=True, order_status='CONFIRMED',
        cash_reconciled=True, position_flat=True)
    row.update(updates)
    return row


class InputIntegrityTests(unittest.TestCase):
    def rejected(self, rows, issue):
        # Both public entry points must retain null strict counts. Test the
        # direct audit too, so a summary-only wrapper cannot hide its failure.
        outputs = [audit_episode_identity(rows, as_of_ms=5000),
                   summarize(rows, as_of_ms=5000)['episode_audit']]
        for result in outputs:
            self.assertEqual(result['status'], 'NOT_EVALUABLE')
            self.assertEqual(result['issues'], [issue])
            for key in ('unique_episode_count', 'supplied_confirmed_entry_episodes',
                        'supplied_cash_closed_episodes', 'nonfinal_episode_count'):
                self.assertIsNone(result[key])
            self.assertIs(result['live_action_taken'], False)
            self.assertIs(result['private_receipts_authenticated'], False)
        return summarize(rows, as_of_ms=5000)

    def test_null_row_does_not_abort_batch(self):
        report = self.rejected([state(), None], 'SNAPSHOT_OBJECT_REQUIRED')
        self.assertEqual(report['record_count'], 2)
        self.assertEqual(report['invalid_record_count'], 1)
        self.assertEqual(report['nonfinal_episodes'], 1)

    def test_non_object_rows_are_retained_as_errors(self):
        for value in ([], 'bad-row', 7, True):
            with self.subTest(value=value):
                self.rejected([value], 'SNAPSHOT_OBJECT_REQUIRED')

    def test_non_array_container_is_not_an_empty_success(self):
        for value in (None, {}, '[]', (state(),)):
            with self.subTest(value=type(value).__name__):
                report = self.rejected(value, 'RECORD_LIST_REQUIRED')
                self.assertIsNone(report['record_count'])
                self.assertIsNone(report['episodes'])

    def test_nan_string_quantity_is_error_not_crash(self):
        self.rejected([state(confirmed_filled_shares='NaN')], 'INVALID_SUPPLIED_QUANTITY')

    def test_infinite_string_quantities_are_errors(self):
        for field in ('confirmed_filled_shares', 'remaining_position_shares',
                      'requested_shares', 'matched_shares'):
            with self.subTest(field=field):
                self.rejected([state(**{field: 'Infinity'})], 'INVALID_SUPPLIED_QUANTITY')

    def test_boolean_quantity_is_not_numeric(self):
        self.rejected([state(confirmed_filled_shares=True)], 'INVALID_SUPPLIED_QUANTITY')

    def test_negative_quantities_rejected(self):
        for field in ('confirmed_filled_shares', 'remaining_position_shares',
                      'requested_shares', 'matched_shares'):
            with self.subTest(field=field):
                self.rejected([state(**{field: '-1'})], 'NEGATIVE_SUPPLIED_QUANTITY')

    def test_confirmed_above_request_is_error_not_crash(self):
        self.rejected([state(confirmed_filled_shares='6')], 'INCONSISTENT_SUPPLIED_QUANTITY')

    def test_zero_requested_quantity_is_invalid(self):
        self.rejected([state(requested_shares='0')], 'INCONSISTENT_SUPPLIED_QUANTITY')

    def test_historical_bad_quantity_is_not_hidden_by_clean_latest(self):
        self.rejected([state(observation_ms=900, confirmed_filled_shares='oops'), state()],
                      'INVALID_SUPPLIED_QUANTITY')

    def test_bad_quantity_not_hidden_by_unknown_flag(self):
        self.rejected([state(reconciliation_unknown=True, confirmed_filled_shares='oops')],
                      'INVALID_SUPPLIED_QUANTITY')

    def test_missing_residual_is_not_zero_for_cash_closure(self):
        row = state(); del row['remaining_position_shares']
        self.rejected([row], 'CASH_CLOSURE_POSITION_QUANTITY_UNKNOWN')

    def test_null_and_blank_residual_are_not_zero(self):
        for value in (None, ''):
            with self.subTest(value=value):
                self.rejected([state(remaining_position_shares=value)],
                              'CASH_CLOSURE_POSITION_QUANTITY_UNKNOWN')

    def test_nonzero_residual_cannot_close_cash(self):
        self.rejected([state(remaining_position_shares='1')], 'CASH_CLOSURE_POSITION_CONFLICT')

    def test_not_flat_cannot_close_cash(self):
        self.rejected([state(position_flat=False)], 'CASH_CLOSURE_POSITION_CONFLICT')

    def test_unterminated_order_cannot_close_cash(self):
        self.rejected([state(order_terminal=False, entry_order_terminal=False)],
                      'CASH_CLOSURE_ORDER_NOT_TERMINAL')

    def test_pending_trade_state_blocks_closure(self):
        for field in ('order_status', 'trade_status'):
            for value in ('MATCHED', 'MINED', 'RETRYING', 'PENDING', 'DELAYED'):
                with self.subTest(field=field, value=value):
                    self.rejected([state(**{field: value})], 'CASH_CLOSURE_TRADE_STATE_CONFLICT')

    def test_trimmed_pending_status_blocks_closure(self):
        self.rejected([state(trade_status=' mined ')], 'CASH_CLOSURE_TRADE_STATE_CONFLICT')

    def test_more_matched_than_confirmed_blocks_closure(self):
        self.rejected([state(matched_shares='7')], 'CASH_CLOSURE_TRADE_STATE_CONFLICT')

    def test_equal_matched_and_confirmed_does_not_invent_pending(self):
        report = summarize([state(matched_shares='5')], as_of_ms=5000)
        self.assertEqual(report['episode_audit']['supplied_cash_closed_episodes'], 1)

    def test_string_boolean_is_not_false_evidence(self):
        for field in ('reconciliation_unknown', 'cash_reconciled', 'position_flat',
                      'order_terminal', 'entry_order_terminal', 'order_submitted'):
            with self.subTest(field=field):
                self.rejected([state(**{field: 'false'})], 'STATE_FLAG_MUST_BE_BOOLEAN')

    def test_integer_boolean_is_not_boolean(self):
        self.rejected([state(reconciliation_unknown=0)], 'STATE_FLAG_MUST_BE_BOOLEAN')

    def test_json_nan_value_not_accepted(self):
        self.rejected([state(other_value=float('nan'))], 'SNAPSHOT_NOT_FINITE_JSON')

    def test_historical_nonfinite_value_not_hidden(self):
        self.rejected([state(observation_ms=900, other_value=float('inf')), state()],
                      'SNAPSHOT_NOT_FINITE_JSON')

    def test_cyclic_input_not_serialized_or_echoed(self):
        row = state(); row['cycle'] = row
        self.rejected([row], 'SNAPSHOT_NOT_FINITE_JSON')

    def test_nonstring_key_rejected(self):
        row = state(); row[1] = 'synthetic'
        self.rejected([row], 'SNAPSHOT_STRING_KEYS_REQUIRED')

    def test_diagnostic_errors_do_not_echo_values(self):
        secret_fixture = 'synthetic-private-marker-do-not-echo'
        report = self.rejected([state(confirmed_filled_shares=secret_fixture)],
                               'INVALID_SUPPLIED_QUANTITY')
        self.assertNotIn(secret_fixture, json.dumps(report))
        self.assertEqual(report['row_validation_issues'],
                         [{'row_index': 0, 'issue': 'INVALID_SUPPLIED_QUANTITY'}])

    def test_valid_rows_not_misreported_as_complete_batch(self):
        report = self.rejected([state(), state(confirmed_filled_shares='bad')],
                               'INVALID_SUPPLIED_QUANTITY')
        self.assertEqual(report['episodes'], 2)
        self.assertTrue(report['legacy_counts_are_row_based'])
        self.assertIsNone(report['episode_audit']['supplied_cash_closed_episodes'])

    def test_input_not_mutated(self):
        rows = [state(), state(confirmed_filled_shares='NaN')]
        before = copy.deepcopy(rows)
        summarize(rows, as_of_ms=5000)
        self.assertEqual(rows, before)

    def test_valid_legacy_row_summary_preserved(self):
        row = {'signal_qualified': False}
        out = summarize([row, row])
        self.assertEqual(out['episodes'], 2)
        self.assertEqual(out['final_episodes'], 2)
        self.assertEqual(out['invalid_record_count'], 0)

    def test_individual_classifier_validation_contract_retained(self):
        with self.assertRaises(ValueError):
            classify(state(confirmed_filled_shares='NaN'))

    def test_latest_unknown_still_supersedes_previous_closed(self):
        report = summarize([state(), state(observation_ms=1100, reconciliation_unknown=True)], as_of_ms=5000)
        self.assertEqual(report['episode_audit']['supplied_cash_closed_episodes'], 0)
        self.assertEqual(report['episode_audit']['nonfinal_episode_count'], 1)

    def test_valid_ten_model_views_remain_one_episode(self):
        rows = [state(strategy_id='model-'+str(i)) for i in range(10)]
        report = summarize(rows, as_of_ms=5000)
        self.assertEqual(report['episode_audit']['supplied_confirmed_entry_episodes'], 1)
        self.assertEqual(report['episode_audit']['supplied_cash_closed_episodes'], 1)
        self.assertEqual(report['invalid_record_count'], 0)

    def test_bad_cash_claim_is_nonfinal_even_in_row_summary(self):
        row = state(); del row['remaining_position_shares']
        report = summarize([row], as_of_ms=5000)
        self.assertEqual(report['invalid_record_count'], 1)
        self.assertEqual(report['final_episodes'], 0)
        self.assertEqual(report['nonfinal_episodes'], 1)

    def test_status_container_not_treated_as_empty_status(self):
        for value in (None, [], {}, 0):
            with self.subTest(value=type(value).__name__):
                self.rejected([state(trade_status=value)], 'TRADE_STATE_MUST_BE_STRING')

    def test_new_unknown_does_not_reuse_stale_missing_residual_claim(self):
        row = state(observation_ms=1100, reconciliation_unknown=True)
        del row['remaining_position_shares']
        report = summarize([state(), row], as_of_ms=5000)['episode_audit']
        self.assertEqual(report['supplied_cash_closed_episodes'], 0)
        self.assertEqual(report['nonfinal_episode_count'], 1)

    def test_empty_list_is_consistent_zero_not_null(self):
        report = summarize([], as_of_ms=5000)
        self.assertEqual(report['episode_audit']['unique_episode_count'], 0)
        self.assertEqual(report['invalid_record_count'], 0)

if __name__ == '__main__':
    unittest.main()
