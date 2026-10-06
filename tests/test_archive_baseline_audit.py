import importlib.util
import tempfile
import unittest
from unittest.mock import patch
import csv
from datetime import datetime, timezone
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "analysis" / "archive_baseline_audit.py"
spec = importlib.util.spec_from_file_location("archive_baseline_audit", MODULE)
audit = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(audit)
FIXTURES = Path(__file__).parent / "fixtures"


class ArchiveBaselineAuditTests(unittest.TestCase):
    def quality_sample(self, rows):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'data/monthly_runs/source/quotes.csv'
            path.parent.mkdir(parents=True)
            with path.open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=['ts_iso', 'slug'] + list(audit.PRICE_FIELDS + audit.SIZE_FIELDS))
                writer.writeheader()
                writer.writerows(rows)
            before = path.read_bytes()
            with patch.object(audit, 'revision', return_value='a' * 40):
                report = audit.audit_quality_only(root, '2026-09-23')
            self.assertEqual(before, path.read_bytes())
            self.assertTrue(report['raw_preserved_hashes_rechecked'])
            return report

    def quality_row(self, offset=1):
        start = int(datetime(2026, 9, 23, tzinfo=timezone.utc).timestamp())
        row = {'ts_iso': datetime.fromtimestamp(start + offset, timezone.utc).isoformat(),
               'slug': f'btc-updown-5m-{start}'}
        row.update(dict(zip(audit.PRICE_FIELDS, (50, 49, 50, 49))))
        row.update({field: 10 for field in audit.SIZE_FIELDS})
        return row

    def test_quality_without_oi_retains_descriptive_quotes_but_no_execution(self):
        report = self.quality_sample([self.quality_row()])
        self.assertEqual(report['descriptive_cleaning']['retained_windows'], 1)
        self.assertEqual(report['strict_execution']['accepted_windows'], 0)
        self.assertFalse(report['oi_dependency']['baseline_requires_oi'])
        self.assertEqual(report['oi_dependency']['oi_rows_present'], 0)
        self.assertIsNone(report['metrics']['pnl'])

    def test_quality_conflict_excludes_entire_window_without_inventing_labels(self):
        row, changed = self.quality_row(), self.quality_row()
        changed['buy_up_cents'] = 51
        report = self.quality_sample([row, self.quality_row(2), changed])
        self.assertEqual(report['descriptive_cleaning']['retained_windows'], 0)
        self.assertEqual(report['descriptive_cleaning']['excluded_windows'], 1)
        self.assertIn('no_independent_resolution_labels', report['strict_execution']['blockers'])

    def test_quality_excludes_nonpositive_touch_and_outside_window_rows(self):
        row = self.quality_row()
        row['buy_up_size'] = 0
        report = self.quality_sample([row, self.quality_row(301)])
        reasons = report['descriptive_cleaning']['row_exclusion_reason_counts_overlap']
        self.assertEqual(reasons['nonpositive_touch_size'], 1)
        self.assertEqual(reasons['sample_outside_window'], 1)
        self.assertEqual(report['descriptive_cleaning']['retained_unique_rows'], 0)

    def test_protocol_rejects_missing_positive_costs(self):
        protocol = {
            "baselines": ["no_trade"], "reference_days": 1, "evaluation_days": 1,
            "decision_offset_seconds": 120, "signal_lookback_seconds": 60,
            "delay_seconds": 3, "hold_seconds": 30, "max_sample_gap_seconds": 5,
            "initial_cash_usd": 10, "slippage_per_leg_usd": 0.01,
            "minimum_shares_scenarios": [5], "fee_per_share_per_leg_usd_scenarios": [0],
        }
        with self.assertRaises(ValueError):
            audit.validate_protocol(protocol)

    def test_market_start_rejects_non_five_minute_slug(self):
        self.assertEqual(audit.market_start({"slug": "btc-updown-5m-1700000001"}), (None, None))
        self.assertEqual(audit.market_start({"slug": "btc-updown-5m-1700000100"})[1], 1700000100)

    def test_quote_replay_is_fail_closed_on_unknown_exit(self):
        start = 1_700_000_100
        rows = [{
            "ts": start + offset,
            "buy_up_cents": 50.0, "sell_up_cents": 49.0,
            "buy_down_cents": 50.0, "sell_down_cents": 49.0,
            "buy_up_size": 10.0, "sell_up_size": 10.0,
            "buy_down_size": 10.0, "sell_down_size": 10.0,
        } for offset in (120, 123)]
        protocol = {
            "initial_cash_usd": 10, "decision_offset_seconds": 120,
            "delay_seconds": 3, "hold_seconds": 30, "max_sample_gap_seconds": 5,
            "slippage_per_leg_usd": 0.01,
        }
        result = audit.replay({"btc-updown-5m-1700000100": rows}, protocol, "fixed_up", 5, 0.01)
        self.assertEqual(result["real_fills"], 0)
        self.assertTrue(result["terminal_unresolved_position"])
        self.assertIn("unknown_exit_full_cost_loss_and_halt", result["status_counts"])

    def test_private_output_must_be_outside_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            audit.write_private(output, {"synthetic": True})
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                audit.write_private(output, {"synthetic": True})

    def test_baseline_only_flattens_contract_validated_v3_snapshots(self):
        self.assertEqual(
            audit.validated_window_dates(FIXTURES / "v3_observation_valid.jsonl"),
            ["2026-10-03"],
        )
        windows, report = audit.load_validated_jsonl(
            FIXTURES / "v3_observation_valid.jsonl", {"2026-09-01"}
        )
        self.assertEqual(windows, {})
        self.assertEqual(report["counts"]["accepted_records"], 1)
        windows, report = audit.load_validated_jsonl(
            FIXTURES / "v3_observation_valid.jsonl", {"2026-10-03"}
        )
        self.assertEqual(report["counts"]["selected_windows"], 1)

    def test_legacy_csv_row_is_quarantined_instead_of_replayed(self):
        import csv

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "legacy.csv"
            with path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["ts_iso", "slug", "buy_up_cents"])
                writer.writeheader()
                writer.writerow({"ts_iso": "2026-10-03T04:00:00Z", "slug": "btc-updown-5m-1791000000", "buy_up_cents": "50"})
            manifest = [{"path": "legacy.csv", "min_ts": 1791000000, "max_ts": 1791000000}]
            windows, report = audit.load_selected(root, manifest, {"2026-10-03"})
            self.assertEqual(windows, {})
            self.assertEqual(report["counts"]["contract_quarantined_rows"], 1)


if __name__ == "__main__":
    unittest.main()
