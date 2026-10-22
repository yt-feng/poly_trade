import copy
import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from analysis.h534_compound_bundle_builder import build_h534_bundle, validate_built_h534_bundle

_fixture_spec = importlib.util.spec_from_file_location(
    "polybolt_bundle_fixture", Path(__file__).with_name("test_validate_polybolt_compound_bundle.py")
)
_fixture_module = importlib.util.module_from_spec(_fixture_spec)
assert _fixture_spec.loader is not None
_fixture_spec.loader.exec_module(_fixture_module)


def capture_fixture():
    document = _fixture_module.bundle()
    document["strategy"] = {
        "id": _fixture_module.H534_STRATEGY_ID,
        "version": _fixture_module.H534_STRATEGY_VERSION,
        "config_sha256": _fixture_module.H534_STRATEGY_CONFIG_SHA256,
    }
    _fixture_module.configure_adaptive_fixture(document, ["0.55"] * 10)
    _fixture_module.attach_strategy_trace(document, ["0.55"] * 10, track_profits=False)
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


class H534CompoundBundleBuilderTests(unittest.TestCase):
    def test_builder_preserves_fixed_fraction_trace(self):
        capture = capture_fixture()
        document = build_h534_bundle(capture)
        self.assertEqual(document["strategy_decisions"][0]["cash_fraction"], "0.55")
        result = validate_built_h534_bundle(capture)
        self.assertTrue(result["offline_consistency_passed"])
        self.assertEqual(result["fills"], 10)

    def test_builder_rejects_nonzero_profit_state(self):
        capture = capture_fixture()
        capture["rows"][0]["strategy_decision"]["profitable_released_exits_before"] = 1
        with self.assertRaisesRegex(ValueError, "strategy_decision_profit_state_invalid"):
            build_h534_bundle(capture)

    def test_builder_rejects_partial_chain(self):
        capture = capture_fixture()
        capture["rows"] = capture["rows"][:9]
        with self.assertRaisesRegex(ValueError, "exactly_ten_rows_required"):
            build_h534_bundle(capture)
