import gzip
import json
import tempfile
import unittest
from pathlib import Path

from analysis.h325_orderfilled_btc5m_source_audit import WINDOW_START, audit


def fill(ts=WINDOW_START + 3, taker="0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e", suffix="1"):
    return {
        "id": "0x" + suffix.zfill(64) + "_0x" + ("2" + suffix).zfill(64),
        "timestamp": str(ts),
        "transactionHash": "0x" + suffix.zfill(64),
        "orderHash": "0x" + ("2" + suffix).zfill(64),
        "maker": "0x471c14910f0eba1b40c163ed6e4b063a8151d89f",
        "taker": taker,
        "makerAssetId": "0",
        "takerAssetId": "123456",
        "makerAmountFilled": "1000000",
        "takerAmountFilled": "2000000",
        "fee": "1000",
    }


class H325Tests(unittest.TestCase):
    def write_window(self, root: Path, start: int, rows):
        with gzip.open(root / f"{start}.fills.jsonl.gz", "wt", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    def test_raw_orderfilled_identity_and_exchange_leg_are_admitted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "input"; out = Path(td) / "out"
            root.mkdir()
            for i in range(12):
                start = WINDOW_START + i * 300
                self.write_window(root, start, [fill(ts=start + 2, suffix=str(i + 1))])
            report = audit(root, out)
            self.assertTrue(report["h325a_identity_coverage_passed"])
            self.assertTrue(report["h325b_execution_fields_available_for_registration"])
            self.assertFalse(report["strategy_evaluated"])
            self.assertEqual(report["verified_canary_fills"], 0)

    def test_missing_window_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "input"; out = Path(td) / "out"
            root.mkdir()
            self.write_window(root, WINDOW_START, [fill()])
            report = audit(root, out)
            self.assertFalse(report["h325a_identity_coverage_passed"])
            self.assertEqual(report["missing_window_count"], 11)

    def test_timestamp_and_asset_pair_errors_do_not_become_valid_rows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "input"; out = Path(td) / "out"
            root.mkdir()
            bad = fill(ts=WINDOW_START + 300)
            bad["takerAssetId"] = "0"
            self.write_window(root, WINDOW_START, [bad])
            report = audit(root, out)
            self.assertEqual(report["rows_total"], 0)
            self.assertEqual(report["invalid_rows"], 1)
            self.assertFalse(report["h325a_identity_coverage_passed"])


if __name__ == "__main__":
    unittest.main()
