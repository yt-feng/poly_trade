"""Offline regressions for consuming known gaps, not new quality-counting tests."""
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'analysis'))
from canary_continuity import GapMask
from canary_core import CausalSignals
from canary_readiness import load, mechanism, replay, run
from canary_shadow import stream
from forward_roll_conflict import run as forward_run
from test_canary import START, fixture, fake_features



def shifted_fixture(ms):
    """Preserve receipt ages while moving the synthetic BTC tape in UTC."""
    second = (ms - START) // 1000
    row = fixture(second)
    row['slug'] = f'btc-updown-5m-{ms // 300000 * 300}'
    row['microstructure']['rules']['slug'] = row['slug']
    return row


def mask(start, end):
    return GapMask([(START + start * 1000, START + end * 1000)])


def signals(rows, gaps=None):
    engine = CausalSignals(gaps)
    return [engine.update(row) for row in rows]


def write_inputs(root, rows, start=70, end=71):
    archive = root / 'snapshots-fixture.jsonl.gz'
    archive.write_bytes(gzip.compress(('\n'.join(map(json.dumps, rows)) + '\n').encode()))
    archive.with_name(archive.name + '.sha256').write_text(hashlib.sha256(archive.read_bytes()).hexdigest())
    report = root / 'quality.json'
    report.write_text(json.dumps({'schema_version': 2, 'quality_contract_version': 3, 'daily': [
        {'asset': 'eth', 'missing_intervals': [{'start_second': START // 1000, 'end_second_exclusive': START // 1000 + 300}]},
        {'asset': 'btc', 'date_utc': datetime.fromtimestamp(START // 1000, timezone.utc).date().isoformat(),
         'expected_seconds': 86400, 'observed_seconds': 86400-(end-start), 'missing_seconds': end-start,
         'missing_intervals': [{'start_second': START // 1000 + start, 'end_second_exclusive': START // 1000 + end}]}
    ]}))
    return report


class SignalContinuity(unittest.TestCase):
    def test_known_single_second_gap_resets_and_rewarms_each_horizon(self):
        rows = [fixture(i) for i in range(132) if i != 70]
        output = {int((f['sample_ms'] - START) / 1000): f for f in signals(rows, mask(70, 71))}
        self.assertAlmostEqual(output[69]['roll_pressure_usd_s'], .2)
        for i in range(71, 131):
            self.assertIsNone(output[i]['roll_pressure_usd_s'])
            self.assertEqual(output[i]['directions']['roll'], 0)
        for i in range(71, 76):
            self.assertIsNone(output[i]['spot_return_5s_bps'])
            self.assertIsNone(output[i]['twap_change_5s'])
        self.assertIsNotNone(output[76]['spot_return_5s_bps'])
        self.assertAlmostEqual(output[131]['roll_pressure_usd_s'], .2)

    def test_unmasked_two_second_delta_is_not_claimed_to_be_a_known_gap(self):
        rows = [fixture(i) for i in range(70)] + [fixture(71)]
        self.assertAlmostEqual(signals(rows)[-1]['roll_pressure_usd_s'], .2)

    def test_near_two_second_jitter_keeps_history(self):
        rows = [fixture(i) for i in range(70)]
        delayed = fixture(70)
        delayed['sample_ms'] += 950
        rows += [delayed, fixture(71)]
        result = signals(rows, mask(170, 171))
        self.assertIsNotNone(result[-2]['roll_pressure_usd_s'])
        self.assertAlmostEqual(result[-1]['roll_pressure_usd_s'], .2)

    def test_gap_straddling_second_boundary_without_evidence_keeps_history(self):
        rows = [fixture(i) for i in range(70)]
        rows[-1]['sample_ms'] -= 1  # 68.999 -> 70.001 is only 1.002 seconds.
        rows.append(fixture(70))
        rows[-1]['sample_ms'] += 1
        self.assertIsNotNone(signals(rows)[-1]['roll_pressure_usd_s'])

    def test_continuous_btc_reference_lookback_can_cross_market_boundary(self):
        rows = [fixture(i) for i in range(316)]
        for row in rows[300:]:
            row['slug'] = f'btc-updown-5m-{START // 1000 + 300}'
            row['microstructure']['rules']['slug'] = row['slug']
        last = signals(rows)[-1]
        self.assertAlmostEqual(last['roll_pressure_usd_s'], .2)
        self.assertIsNotNone(last['twap_change_5s'])

    def test_full_new_window_cannot_hide_gap_in_previous_window_lookback(self):
        rows = [fixture(i) for i in range(316) if i != 298]
        for row in rows:
            if row['sample_ms'] >= START + 300000:
                row['slug'] = f'btc-updown-5m-{START // 1000 + 300}'
                row['microstructure']['rules']['slug'] = row['slug']
        self.assertIsNotNone(signals(rows)[-1]['roll_pressure_usd_s'])
        self.assertIsNone(signals(rows, mask(298, 299))[-1]['roll_pressure_usd_s'])

    def test_reference_history_does_not_cross_assets(self):
        rows = [fixture(i) for i in range(71)]
        rows[-1]['asset'] = 'eth'
        self.assertIsNone(signals(rows)[-1]['roll_pressure_usd_s'])

    def test_future_mask_never_changes_a_past_signal(self):
        rows = [fixture(i) for i in range(70)]
        self.assertEqual(signals(rows), signals(rows, mask(70, 71)))

    def test_masked_snapshot_fails_closed_and_end_is_usable(self):
        engine = CausalSignals(mask(70, 71))
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            engine.update(fixture(70))
        self.assertIsNone(engine.last_ms)
        self.assertIsNone(engine.update(fixture(71))['roll_pressure_usd_s'])


class FutureDependencies(unittest.TestCase):
    def test_future_probe_cannot_bridge_known_missing_second(self):
        rows = [fixture(i) for i in range(90) if i != 63]
        feats = signals(rows, mask(63, 64))
        before = mechanism(rows, feats)
        after = mechanism(rows, feats, mask(63, 64))
        self.assertIn(START + 60000, [p['decision_ms'] for p in before])
        self.assertNotIn(START + 60000, [p['decision_ms'] for p in after])

    def test_future_probe_cannot_cross_an_intermediate_foreign_market(self):
        rows = [fixture(i) for i in range(90)]
        feats = signals(rows)
        rows[63]['slug'] = 'btc-updown-5m-other'
        self.assertNotIn(START + 60000, [p['decision_ms'] for p in mechanism(rows, feats)])

    def test_taker_arrival_path_is_masked(self):
        rows = [fixture(i) for i in range(40) if i != 1]
        feats = fake_features(rows)
        self.assertEqual(replay(rows, feats, 'roll', 15, 1, 'taker')[0]['status'], 'valued')
        result = replay(rows, feats, 'roll', 15, 1, 'taker', mask(1, 2))[0]
        self.assertEqual(result['status'], 'entry_path_gap')
        self.assertNotIn('entry_ms', result)

    def test_exit_gap_retains_unknown_exit_and_full_cost_bound(self):
        rows = [fixture(i) for i in range(40) if i != 8]
        feats = fake_features(rows)
        result = replay(rows, feats, 'roll', 15, 1, 'taker', mask(8, 9))[0]
        self.assertEqual(result['status'], 'exit_path_gap')
        self.assertIn('entry_ms', result)
        self.assertNotIn('pnl_usd', result)
        self.assertAlmostEqual(result['pnl_lower_bound_usd'], -2.5875)

    def test_maker_wait_cannot_bridge_missing_second(self):
        rows = [fixture(i) for i in range(40) if i != 3]
        rows[3]['up'].update(bid=.47, ask=.48)  # fixture(4), after the gap
        feats = fake_features(rows)
        self.assertIn('entry_ms', replay(rows, feats, 'roll', 15, 1, 'maker_trade_through_proxy')[0])
        result = replay(rows, feats, 'roll', 15, 1, 'maker_trade_through_proxy', mask(3, 4))[0]
        self.assertEqual(result['status'], 'entry_path_gap')
        self.assertNotIn('entry_ms', result)

    def test_exit_cannot_cross_intermediate_foreign_market(self):
        rows = [fixture(i) for i in range(40)]
        rows[8]['slug'] = 'btc-updown-5m-other'
        # Avoid treating the synthetic intermediate slug as a new decision.
        feats = fake_features(rows)
        feats[8]['directions'] = {k: 0 for k in feats[8]['directions']}
        self.assertEqual(replay(rows, feats, 'roll', 15, 1, 'taker')[0]['status'], 'exit_path_gap')


class MaskIntegration(unittest.TestCase):

    def test_loader_consumes_btc_mask_and_rejects_conflicting_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = write_inputs(root, [fixture(69), fixture(71)])
            gaps = GapMask.from_report(report)
            rows, cov = load(root, gaps)
            self.assertEqual(len(rows), 2)
            self.assertEqual(cov['known_gap_intervals'], 1)
            self.assertTrue(cov['quality_report_supplied'])
            self.assertEqual(cov['quality_report_sha256'], hashlib.sha256(report.read_bytes()).hexdigest())
            write_inputs(root, [fixture(70)])
            with self.assertRaisesRegex(ValueError, 'conflicts'):
                load(root, gaps)

    def test_readiness_and_shadow_receive_the_same_mask(self):
        rows = [fixture(i) for i in range(70)] + [fixture(71)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = write_inputs(root, rows)
            with patch('builtins.print'):
                run(root, root / 'output', report)
            decisions = [json.loads(s) for s in (root / 'output/shadow_decisions.jsonl').read_text().splitlines()]
            output = io.StringIO()
            stream(io.StringIO('\n'.join(map(json.dumps, rows))), output, GapMask.from_report(report))
            shadow = json.loads(output.getvalue().splitlines()[-1])
            self.assertIsNone(decisions[-1]['roll_pressure_usd_s'])
            self.assertEqual(decisions[-1]['directions'], shadow['directions'])

    def test_forward_entrypoint_passes_mask_to_features_and_replay(self):
        rows = [fixture(i) for i in range(70)] + [fixture(71)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = write_inputs(root, rows)
            with patch('forward_roll_conflict.replay', wraps=replay) as replay_call:
                result = forward_run(root, root / 'output', quality_report=report)
            self.assertTrue(result['coverage']['quality_report_supplied'])
            for call in replay_call.call_args_list:
                self.assertIsNone(call.args[1][-1]['roll_pressure_usd_s'])
                self.assertTrue(call.args[-1].intersects(START + 70000, START + 70000))

    def test_invalid_or_wrong_asset_mask_cannot_be_silently_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'quality.json'
            for report in ({'daily': [{'asset': 'eth', 'missing_intervals': []}]},
                           {'daily': [{'asset': 'btc', 'missing_intervals': [{'start_second': True, 'end_second_exclusive': 3}]}]}):
                path.write_text(json.dumps(report))
                with self.assertRaises(ValueError):
                    GapMask.from_report(path)


if __name__ == '__main__':
    unittest.main()
