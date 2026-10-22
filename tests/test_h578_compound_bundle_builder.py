import copy
import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from analysis.h578_compound_bundle_builder import (
    build_h578_bundle,
    validate_built_h578_bundle,
)
_fixture_spec = importlib.util.spec_from_file_location(
    "polybolt_bundle_fixture",
    Path(__file__).with_name("test_validate_polybolt_compound_bundle.py"),
)
_fixture_module = importlib.util.module_from_spec(_fixture_spec)
assert _fixture_spec.loader is not None
_fixture_spec.loader.exec_module(_fixture_module)

H578_STRATEGY_CONFIG_SHA256 = _fixture_module.H578_STRATEGY_CONFIG_SHA256
H578_STRATEGY_ID = _fixture_module.H578_STRATEGY_ID
H578_STRATEGY_VERSION = _fixture_module.H578_STRATEGY_VERSION
attach_depth_proxy_trace = _fixture_module.attach_depth_proxy_trace
bundle = _fixture_module.bundle
configure_adaptive_fixture = _fixture_module.configure_adaptive_fixture


def capture_fixture():
    document = bundle()
    document["strategy"] = {
        "id": H578_STRATEGY_ID,
        "version": H578_STRATEGY_VERSION,
        "config_sha256": H578_STRATEGY_CONFIG_SHA256,
    }
    configure_adaptive_fixture(document, ["0.75"] * 10)
    for snapshot in document["depth_snapshots"]:
        snapshot["sell_available_shares_2m"] = "90"
    attach_depth_proxy_trace(document, depth="90", cash_fraction="0.75")
    rows = copy.deepcopy(document["events"])
    traces = {item["sequence_no"]: item for item in document["strategy_decisions"]}
    for row in rows:
        row["strategy_decision"] = traces[row["sequence_no"]]
    return {
        **{key: document[key] for key in (
            "owner", "ledger_id", "chain_id", "starting_checkpoint_id",
            "closing_balance_checkpoint_id", "initial_cash", "closing_balance",
            "market_windows", "source_events", "depth_snapshots",
            "settlement_receipts", "cash_release_receipts",
        )},
        "rows": rows,
        "strategy": document["strategy"],
    }


class H578CompoundBundleBuilderTests(unittest.TestCase):
    def test_builder_maps_taker_adapter_timestamps_and_validates_h578(self):
        capture = capture_fixture()
        for row in capture["rows"]:
            row["entry_ts"] = row.pop("entry_ts_ms")
            row["cash_available_ts"] = row.pop("cash_available_ts_ms")
        document = build_h578_bundle(capture)
        self.assertEqual(document["required_fills"], 10)
        self.assertEqual(document["events"][0]["entry_ts_ms"], capture["rows"][0]["entry_ts"])
        result = validate_built_h578_bundle(capture)
        self.assertTrue(result["offline_consistency_passed"])
        self.assertEqual(result["fills"], 10)

    def test_builder_rejects_missing_depth_trace(self):
        capture = capture_fixture()
        capture["rows"][0]["strategy_decision"].pop("entry_sell_depth_shares", None)
        with self.assertRaisesRegex(ValueError, "entry_sell_depth_shares_required"):
            build_h578_bundle(capture)

    def test_builder_rejects_reset_or_partial_row_set(self):
        capture = capture_fixture()
        capture["rows"] = copy.deepcopy(capture["rows"][:9])
        with self.assertRaisesRegex(ValueError, "exactly_ten_rows_required"):
            build_h578_bundle(capture)
