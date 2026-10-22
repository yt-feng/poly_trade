import copy
import importlib.util
import unittest
from pathlib import Path

from analysis.h600_compound_bundle_builder import build_h600_bundle, validate_built_h600_bundle

_fixture_spec = importlib.util.spec_from_file_location(
    "polybolt_bundle_fixture", Path(__file__).with_name("test_validate_polybolt_compound_bundle.py")
)
_fixture_module = importlib.util.module_from_spec(_fixture_spec)
assert _fixture_spec.loader is not None
_fixture_spec.loader.exec_module(_fixture_module)


def capture_fixture():
    document = _fixture_module.bundle()
    document["strategy"] = {
        "id": _fixture_module.H600_STRATEGY_ID,
        "version": _fixture_module.H600_STRATEGY_VERSION,
        "config_sha256": _fixture_module.H600_STRATEGY_CONFIG_SHA256,
    }
    _fixture_module.configure_adaptive_fixture(document, ["0.75"] * 10)
    for snapshot in document["depth_snapshots"]:
        snapshot["sell_available_shares_2m"] = "90"
    _fixture_module.attach_favorite_depth_trace(document, depth="90", favorite_ask="0.50", cash_fraction="0.75")
    rows = copy.deepcopy(document["events"])
    traces = {item["sequence_no"]: item for item in document["strategy_decisions"]}
    for row in rows:
        row["strategy_decision"] = traces[row["sequence_no"]]
    return {
        **{key: document[key] for key in (
            "owner", "ledger_id", "chain_id", "starting_checkpoint_id", "closing_balance_checkpoint_id",
            "initial_cash", "closing_balance", "market_windows", "source_events", "depth_snapshots",
            "settlement_receipts", "cash_release_receipts",
        )},
        "rows": rows,
        "strategy": document["strategy"],
    }


class H600CompoundBundleBuilderTests(unittest.TestCase):
    def test_builder_preserves_favorite_ask_in_hashed_trace(self):
        capture = capture_fixture()
        document = build_h600_bundle(capture)
        self.assertEqual(document["strategy_decisions"][0]["favorite_ask_price"], "0.50")
        result = validate_built_h600_bundle(capture)
        self.assertTrue(result["offline_consistency_passed"])
        self.assertEqual(result["fills"], 10)

    def test_builder_rejects_plain_h578_trace(self):
        capture = capture_fixture()
        capture["rows"][0]["strategy_decision"].pop("favorite_ask_price")
        with self.assertRaisesRegex(ValueError, "favorite_ask_price_required"):
            build_h600_bundle(capture)

    def test_builder_rejects_partial_chain(self):
        capture = capture_fixture()
        capture["rows"] = capture["rows"][:9]
        with self.assertRaisesRegex(ValueError, "exactly_ten_rows_required"):
            build_h600_bundle(capture)
