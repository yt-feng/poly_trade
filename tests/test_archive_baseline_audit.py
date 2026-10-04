import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "analysis" / "archive_baseline_audit.py"
spec = importlib.util.spec_from_file_location("archive_baseline_audit", MODULE)
audit = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(audit)
FIXTURES = Path(__file__).parent / "fixtures"


class ArchiveBaselineAuditTests(unittest.TestCase):
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
