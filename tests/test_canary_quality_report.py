"""Consumer contract and offline CLI failures for explicit quality reports."""
import copy
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'analysis'))
from canary_continuity import GapMask
from canary_readiness import run
from test_canary import START, fixture
from test_canary_continuity import shifted_fixture, write_inputs

ROOT = Path(__file__).resolve().parents[1]


class QualityReportContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rows = [fixture(i) for i in range(70)] + [fixture(71)]
        self.path = write_inputs(self.root, self.rows)
        self.good = json.loads(self.path.read_text())

    def save(self, report):
        self.path.write_text(json.dumps(report))

    def cli(self, name, report=True, rows=None, suffix=''):
        rows = self.rows if rows is None else rows
        source = self.root / 'input.jsonl'
        source.write_text('\n'.join(map(json.dumps, rows)))
        output = self.root / (name + suffix)
        command = [sys.executable, str(ROOT / 'analysis' / (name + '.py')),
                   '--input', str(source if name == 'canary_shadow' else self.root), '--output', str(output)]
        if report:command += ['--quality-report', str(self.path)]
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=15)
        return result, output

    def invoke_all(self, raw=None, rows=None):
        for name in ('canary_readiness', 'canary_shadow', 'forward_roll_conflict'):
            with self.subTest(entrypoint=name):
                # Write only input archives here; do not overwrite the report under test.
                data = ('\n'.join(map(json.dumps, self.rows if rows is None else rows))+'\n').encode()
                archive = self.root / 'snapshots-fixture.jsonl.gz'
                archive.write_bytes(gzip.compress(data))
                archive.with_name(archive.name+'.sha256').write_text(hashlib.sha256(archive.read_bytes()).hexdigest())
                source = self.root / 'input.jsonl'; source.write_bytes(data)
                if raw is None:self.path.unlink(missing_ok=True)
                else:self.path.write_bytes(raw)
                command = [sys.executable, str(ROOT/'analysis'/(name+'.py')), '--input',
                           str(source if name == 'canary_shadow' else self.root),
                           '--output', str(self.root/(name+'-invalid')), '--quality-report', str(self.path)]
                result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=15)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn('Error', result.stderr)

    def test_missing_report_fails_all_entrypoints(self):
        self.invoke_all()

    def test_corrupt_json_failure_schema_and_duplicate_fields_fail_all_entrypoints(self):
        for raw in (b'{broken', b'\xff', b'null', b'{"daily":[],"daily":[]}',
                    b'{"schema_version":1,"report_kind":"daily_quality_failure","status":"failed"}'):
            with self.subTest(raw=raw):self.invoke_all(raw)

    def test_malformed_report_fields_are_rejected(self):
        mutations = [lambda r: r.update(schema_version=2.0), lambda r: r.update(quality_contract_version=2),
                     lambda r: r.update(daily={}), lambda r: r.update(daily=[None]),
                     lambda r: r.update(quality_report_complete=False),
                     lambda r: r['daily'][1].pop('date_utc'),
                     lambda r: r['daily'][1].update(date_utc='2026-02-30'),
                     lambda r: r['daily'][1].update(date_utc='20260923'),
                     lambda r: r['daily'][1].update(observed_seconds=True),
                     lambda r: r['daily'][1].update(missing_seconds=0),
                     lambda r: r['daily'][1].update(missing_intervals=''),
                     lambda r: r['daily'][1]['missing_intervals'][0].update(duration_seconds=2),
                     lambda r: r['daily'].append(copy.deepcopy(r['daily'][1])),
                     lambda r: r['daily'][1]['missing_intervals'].append(copy.deepcopy(r['daily'][1]['missing_intervals'][0]))]
        for mutate in mutations:
            report = copy.deepcopy(self.good); mutate(report); self.save(report)
            with self.subTest(report=report), self.assertRaises(ValueError):GapMask.from_report(self.path)

    def test_report_date_and_interval_must_agree(self):
        report = copy.deepcopy(self.good); report['daily'][1]['date_utc'] = '2026-10-12'; self.save(report)
        with self.assertRaisesRegex(ValueError, 'UTC-day'):GapMask.from_report(self.path)

    def test_valid_but_unrelated_date_fails_all_entrypoints(self):
        report = copy.deepcopy(self.good); day = report['daily'][1]
        day['date_utc'] = datetime.fromtimestamp(START // 1000 + 86400, timezone.utc).date().isoformat()
        for gap in day['missing_intervals']:
            gap['start_second'] += 86400; gap['end_second_exclusive'] += 86400
        self.invoke_all(json.dumps(report).encode())

    def test_partial_date_coverage_fails_all_entrypoints(self):
        self.invoke_all(json.dumps(self.good).encode(), [fixture(0), shifted_fixture(START+86400000)])

    def test_wrong_asset_report_fails_all_entrypoints(self):
        report = copy.deepcopy(self.good); report['daily'] = report['daily'][:1]
        self.invoke_all(json.dumps(report).encode())

    def test_wrong_market_or_timestamp_window_fails_all_entrypoints(self):
        for mutation in ({'slug': f'eth-updown-5m-{START//1000}'},
                         {'slug': f'btc-updown-5m-{START//1000+300}'},
                         {'window_start_ms': START+300000}):
            row = fixture(0); row.update(mutation)
            with self.subTest(mutation=mutation):self.invoke_all(json.dumps(self.good).encode(), [row])

    def test_millisecond_gap_boundaries_and_fractional_timestamp(self):
        gaps = GapMask.from_report(self.path)
        start, end = gaps.intervals[0]
        for ms in (start-1, end):
            row = fixture(69); row['sample_ms'] = ms; gaps.check_row(row)
        for ms in (start, end-1):
            row = fixture(69); row['sample_ms'] = ms
            with self.assertRaisesRegex(ValueError, 'conflicts'):gaps.check_row(row)
        for ms in (float(start), True, -1):
            row = fixture(69); row['sample_ms'] = ms
            with self.assertRaisesRegex(ValueError, 'integer'):gaps.check_row(row)

    def test_midnight_coverage_is_half_open(self):
        report = copy.deepcopy(self.good); day = report['daily'][1]
        day.update(missing_intervals=[], observed_seconds=86400, missing_seconds=0)
        self.save(report); gaps = GapMask.from_report(self.path)
        midnight = (START // 86400000 + 1) * 86400000
        before = shifted_fixture(midnight-1000); before['sample_ms'] = midnight-1
        gaps.check_row(before)
        with self.assertRaisesRegex(ValueError, 'does not cover'):gaps.check_row(shifted_fixture(midnight))
        tomorrow = copy.deepcopy(day)
        tomorrow['date_utc'] = datetime.fromtimestamp(midnight//1000, timezone.utc).date().isoformat()
        report['daily'].append(tomorrow); self.save(report)
        GapMask.from_report(self.path).check_row(shifted_fixture(midnight))

    def test_all_entrypoints_report_same_conditional_scope(self):
        for supplied in (False, True):
            metadata = []
            for name in ('canary_readiness', 'canary_shadow', 'forward_roll_conflict'):
                result, output = self.cli(name, report=supplied, suffix=str(supplied))
                self.assertEqual(result.returncode, 0, result.stderr)
                if name == 'canary_shadow':scope = json.loads(output.read_text().splitlines()[-1])['quality_report']
                elif name == 'canary_readiness':scope = json.loads((output/'coverage.json').read_text())
                else:scope = json.loads((output/'forward_readiness.json').read_text())['coverage']
                fields = ('quality_report_supplied','known_gap_intervals','quality_report_covered_utc_days',
                          'gap_protection_scope','full_history_complete')
                metadata.append({k:scope[k] for k in fields})
            self.assertEqual(metadata[0], metadata[1]); self.assertEqual(metadata[1], metadata[2])
            self.assertFalse(metadata[0]['full_history_complete'])
            self.assertEqual(metadata[0]['quality_report_supplied'], supplied)
            self.assertEqual(metadata[0]['gap_protection_scope'], 'reported_btc_days_only' if supplied else 'no_known_gap_mask')

    def test_future_resolution_label_cannot_change_decisions_or_attempts(self):
        with patch('builtins.print'):run(self.root, self.root/'before', self.path)
        label = {'kind':'market_resolved','available_at_ms': START+301000,
                 'payload':{'event_type':'market_resolved','market':'condition','assets_ids':['up-token','down-token'],
                            'winning_asset_id':'up-token','winning_outcome':'Up'}}
        archive = self.root/'labels-future.jsonl.gz'; archive.write_bytes(gzip.compress((json.dumps(label)+'\n').encode()))
        archive.with_name(archive.name+'.sha256').write_text(hashlib.sha256(archive.read_bytes()).hexdigest())
        with patch('builtins.print'):run(self.root, self.root/'after', self.path)
        for name in ('shadow_decisions.jsonl', 'attempts.csv'):
            self.assertEqual((self.root/'before'/name).read_bytes(), (self.root/'after'/name).read_bytes())
        self.assertNotEqual((self.root/'before/official_labels.json').read_bytes(),
                            (self.root/'after/official_labels.json').read_bytes())


if __name__ == '__main__':unittest.main()
