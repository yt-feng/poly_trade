import copy
import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from analysis.h600_compound_bundle_builder import (
    H600CompoundCapture,
    build_h600_bundle,
    validate_built_h600_bundle,
)
from analysis.validate_polybolt_compound_bundle import validate_bundle_document

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
        row["orderfilled_event_id"] = f"tx-{row['sequence_no']}:{row['sequence_no']}"
    return {
        **{key: document[key] for key in (
            "owner", "ledger_id", "chain_id", "starting_checkpoint_id", "closing_balance_checkpoint_id",
            "initial_cash", "closing_balance", "market_windows", "source_events", "depth_snapshots",
            "settlement_receipts", "cash_release_receipts", "orderfilled_records",
        )},
        "rows": rows,
        "strategy": document["strategy"],
    }


def opening_header(capture):
    return {key: copy.deepcopy(capture[key]) for key in (
        "owner", "ledger_id", "chain_id", "starting_checkpoint_id", "initial_cash", "strategy",
    )}


def evidence_for_row(capture, index):
    return {
        "market_windows": copy.deepcopy(capture["market_windows"]) if index == 0 else [],
        **{key: [copy.deepcopy(capture[key][index])] for key in (
            "source_events", "depth_snapshots", "settlement_receipts", "cash_release_receipts",
        )},
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

    def test_builder_rejects_incomplete_execution_identity_before_document_build(self):
        for field in ("order_id", "order_status", "trade_id", "transaction_ref", "orderfilled_event_id", "settlement_receipt_id"):
            capture = capture_fixture()
            capture["rows"][0].pop(field)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, f"{field}_required"):
                build_h600_bundle(capture)

    def test_builder_rejects_missing_canonical_orderfilled_records(self):
        capture = capture_fixture()
        capture.pop("orderfilled_records")
        with self.assertRaisesRegex(ValueError, "orderfilled_records_required"):
            build_h600_bundle(capture)

    def test_builder_rejects_malformed_canonical_orderfilled_records_before_document_build(self):
        capture = capture_fixture()
        capture["orderfilled_records"][0]["tx_hash"] = "tx-other"
        with self.assertRaisesRegex(ValueError, "orderfilled_event_id_mismatch"):
            build_h600_bundle(capture)

    def test_capture_buffer_builds_one_continuous_ten_row_chain(self):
        capture = capture_fixture()
        buffer = H600CompoundCapture(capture)
        for row in capture["rows"]:
            buffer.append(row)
        document = buffer.build()
        self.assertEqual([row["sequence_no"] for row in document["events"]], list(range(1, 11)))
        result = validate_bundle_document(document, strategy_profile=_fixture_module.H600_STRATEGY_PROFILE)
        self.assertTrue(result["offline_consistency_passed"])
        self.assertEqual(result["fills"], 10)

    def test_capture_buffer_rejects_reset_or_concatenation(self):
        capture = capture_fixture()
        buffer = H600CompoundCapture(capture)
        buffer.append(capture["rows"][0])
        reset = copy.deepcopy(capture["rows"][1])
        reset["previous_checkpoint_id"] = capture["starting_checkpoint_id"]
        with self.assertRaisesRegex(ValueError, "previous_checkpoint_link_mismatch"):
            buffer.append(reset)
        for row in capture["rows"][1:]:
            buffer.append(row)
        with self.assertRaisesRegex(ValueError, "capture_already_complete"):
            buffer.append(capture["rows"][0])

    def test_capture_buffer_rejects_incomplete_execution_provenance_early(self):
        capture = capture_fixture()
        buffer = H600CompoundCapture(capture)
        incomplete = copy.deepcopy(capture["rows"][0])
        incomplete.pop("depth_ref")
        with self.assertRaisesRegex(ValueError, "depth_ref_required"):
            buffer.append(incomplete)

        incomplete = copy.deepcopy(capture["rows"][0])
        incomplete.pop("strategy_decision")
        with self.assertRaisesRegex(ValueError, "strategy_decision_required"):
            buffer.append(incomplete)

    def test_capture_buffer_rejects_orderfilled_mismatch_before_accumulating_rows(self):
        capture = capture_fixture()
        buffer = H600CompoundCapture(capture)
        mismatched = copy.deepcopy(capture["rows"][0])
        mismatched["transaction_ref"] = "tx-other"
        with self.assertRaisesRegex(ValueError, "orderfilled_transaction_mismatch"):
            buffer.append(mismatched)
        self.assertEqual(buffer.rows, ())

        unknown = copy.deepcopy(capture["rows"][0])
        unknown["orderfilled_event_id"] = "unknown:0"
        with self.assertRaisesRegex(ValueError, "orderfilled_event_not_in_bundle"):
            buffer.append(unknown)
        self.assertEqual(buffer.rows, ())

    def test_empty_capture_progresses_one_to_ten_without_future_evidence(self):
        capture = capture_fixture()
        buffer = H600CompoundCapture(opening_header(capture))
        self.assertEqual(buffer.rows, ())
        self.assertEqual(buffer.orderfilled_records, ())
        with self.assertRaisesRegex(ValueError, "exactly_ten_rows_required"):
            buffer.build()
        for index, row in enumerate(capture["rows"]):
            buffer.append(row, orderfilled_record=capture["orderfilled_records"][index],
                          evidence=evidence_for_row(capture, index))
            self.assertEqual(len(buffer.rows), index + 1)
            self.assertEqual(len(buffer.orderfilled_records), index + 1)
            if index < 9:
                with self.assertRaisesRegex(ValueError, "exactly_ten_rows_required"):
                    buffer.build()
        with self.assertRaisesRegex(ValueError, "closing_balance_checkpoint_id_required"):
            buffer.build()
        document = buffer.build(closing_balance=capture["closing_balance"],
                                closing_balance_checkpoint_id=capture["closing_balance_checkpoint_id"])
        result = validate_bundle_document(document, strategy_profile=_fixture_module.H600_STRATEGY_PROFILE)
        self.assertTrue(result["offline_consistency_passed"])
        self.assertFalse(result["gate_passed"])
        self.assertFalse(result["receipt_authenticity_verified"])
        self.assertEqual(result["actual_fills"], 0)
        self.assertEqual(document, build_h600_bundle(capture))
        with self.assertRaisesRegex(ValueError, "capture_already_complete"):
            buffer.append(capture["rows"][0], orderfilled_record=capture["orderfilled_records"][0])
        self.assertEqual(len(buffer.rows), 10)
        self.assertEqual(len(buffer.orderfilled_records), 10)

    def test_capture_rejects_malformed_new_record_without_mutating_evidence(self):
        capture = capture_fixture()
        buffer = H600CompoundCapture(opening_header(capture))
        bad_record = copy.deepcopy(capture["orderfilled_records"][0])
        bad_record["cash_amount"] = "0.01"
        with self.assertRaisesRegex(ValueError, "orderfilled_cash_amount_mismatch"):
            buffer.append(capture["rows"][0], orderfilled_record=bad_record,
                          evidence=evidence_for_row(capture, 0))
        self.assertEqual(buffer.rows, ())
        self.assertEqual(buffer.orderfilled_records, ())
        # Same identities and evidence can be retried: the rejected append
        # must not reserve an event, source, checkpoint, or receipt identity.
        buffer.append(capture["rows"][0], orderfilled_record=capture["orderfilled_records"][0],
                      evidence=evidence_for_row(capture, 0))
        self.assertEqual(len(buffer.rows), 1)
        self.assertEqual(len(buffer.orderfilled_records), 1)

    def test_late_chronology_failure_does_not_poison_valid_retry(self):
        capture = capture_fixture()
        buffer = H600CompoundCapture(opening_header(capture))
        buffer.append(capture["rows"][0], orderfilled_record=capture["orderfilled_records"][0],
                      evidence=evidence_for_row(capture, 0))
        for timestamp, error in (
            (capture["rows"][0]["entry_ts_ms"], "entry_timestamps_must_increase"),
            (capture["rows"][0]["cash_available_ts_ms"], "cash_release_timestamp_must_precede_next_entry"),
        ):
            invalid = copy.deepcopy(capture["rows"][1])
            invalid["entry_ts_ms"] = timestamp
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                buffer.append(invalid, orderfilled_record=capture["orderfilled_records"][1],
                              evidence=evidence_for_row(capture, 1))
            self.assertEqual(len(buffer.rows), 1)
            self.assertEqual(len(buffer.orderfilled_records), 1)
        buffer.append(capture["rows"][1], orderfilled_record=capture["orderfilled_records"][1],
                      evidence=evidence_for_row(capture, 1))
        self.assertEqual(len(buffer.rows), 2)

    def test_incremental_capture_preserves_owner_ledger_and_identity_guards(self):
        capture = capture_fixture()
        for field in ("owner", "ledger_id", "chain_id"):
            buffer = H600CompoundCapture(opening_header(capture))
            row = copy.deepcopy(capture["rows"][0])
            row[field] = "different"
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, f"{field}_mismatch"):
                buffer.append(row, orderfilled_record=capture["orderfilled_records"][0])
            self.assertEqual(buffer.orderfilled_records, ())
            buffer.append(capture["rows"][0], orderfilled_record=capture["orderfilled_records"][0])
            with self.assertRaisesRegex(ValueError, "duplicate_orderfilled_event_id"):
                buffer.append(capture["rows"][1], orderfilled_record=capture["orderfilled_records"][0])
            self.assertEqual(len(buffer.rows), 1)

    def test_incremental_evidence_and_trace_rejection_is_atomic(self):
        capture = capture_fixture()
        buffer = H600CompoundCapture(opening_header(capture))
        invalid = copy.deepcopy(capture["rows"][0])
        invalid["strategy_decision"]["sequence_no"] = 2
        with self.assertRaisesRegex(ValueError, "strategy_decision_identity_mismatch"):
            buffer.append(invalid, orderfilled_record=capture["orderfilled_records"][0],
                          evidence=evidence_for_row(capture, 0))
        malformed = evidence_for_row(capture, 0)
        malformed["cash_release_receipts"].append(copy.deepcopy(malformed["cash_release_receipts"][0]))
        with self.assertRaisesRegex(ValueError, "cash_release_receipts_identity_reused"):
            buffer.append(capture["rows"][0], orderfilled_record=capture["orderfilled_records"][0], evidence=malformed)
        self.assertEqual(buffer.rows, ())
        self.assertEqual(buffer.orderfilled_records, ())
        buffer.append(capture["rows"][0], orderfilled_record=capture["orderfilled_records"][0],
                      evidence=evidence_for_row(capture, 0))

    def test_final_build_validates_supplied_closing_balance_and_full_evidence(self):
        capture = capture_fixture()
        buffer = H600CompoundCapture(capture)
        for row in capture["rows"]:
            buffer.append(row)
        with self.assertRaisesRegex(ValueError, "closing_balance"):
            buffer.build(closing_balance="999")
        # A rejected finalization must not overwrite the captured closing data.
        self.assertEqual(buffer.build(), build_h600_bundle(capture))
        missing = copy.deepcopy(capture)
        missing["settlement_receipts"] = missing["settlement_receipts"][:9]
        incomplete = H600CompoundCapture(missing)
        for row in missing["rows"]:
            incomplete.append(row)
        with self.assertRaisesRegex(ValueError, "exactly_ten_settlement_receipts_required"):
            incomplete.build()

    def test_final_builder_still_requires_ten_canonical_records(self):
        capture = capture_fixture()
        for count in (0, 1, 9):
            incomplete = copy.deepcopy(capture)
            incomplete["orderfilled_records"] = incomplete["orderfilled_records"][:count]
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, "exactly_ten_orderfilled_records_required"):
                build_h600_bundle(incomplete)

    def test_incremental_capture_rejects_more_than_ten_records(self):
        capture = capture_fixture()
        capture["orderfilled_records"].append(copy.deepcopy(capture["orderfilled_records"][0]))
        with self.assertRaisesRegex(ValueError, "at_most_ten_orderfilled_records_allowed"):
            H600CompoundCapture(capture)

    def test_delayed_evidence_can_finalize_ten_rows_without_recapturing(self):
        capture = capture_fixture()
        buffer = H600CompoundCapture(opening_header(capture))
        for index, row in enumerate(capture["rows"]):
            buffer.append(row, orderfilled_record=capture["orderfilled_records"][index])
        before_rows = buffer.rows
        before_records = buffer.orderfilled_records
        closing = {
            "closing_balance": capture["closing_balance"],
            "closing_balance_checkpoint_id": capture["closing_balance_checkpoint_id"],
        }
        with self.assertRaises(ValueError):
            buffer.build(**closing)
        evidence = {key: copy.deepcopy(capture[key]) for key in (
            "market_windows", "source_events", "depth_snapshots", "settlement_receipts", "cash_release_receipts",
        )}
        delayed_receipt = evidence["cash_release_receipts"].pop()
        buffer.attach_evidence(evidence)
        with self.assertRaisesRegex(ValueError, "exactly_ten_cash_release_receipts_required"):
            buffer.build(**closing)
        buffer.attach_evidence({"cash_release_receipts": [delayed_receipt]})
        self.assertEqual(buffer.build(**closing), build_h600_bundle(capture))
        self.assertEqual(buffer.rows, before_rows)
        self.assertEqual(buffer.orderfilled_records, before_records)

    def test_delayed_evidence_rejection_is_atomic_and_cannot_change_rows(self):
        capture = capture_fixture()
        buffer = H600CompoundCapture(capture)
        for row in capture["rows"]:
            buffer.append(row)
        baseline = buffer.build()
        # The valid first collection must not persist if a later collection
        # fails. A valid document after each rejection proves full rollback.
        extra_window = copy.deepcopy(capture["market_windows"][0])
        extra_window["market_id"] = "extra-market"
        for invalid, expected in (
            ({"market_windows": [extra_window], "cash_release_receipts": [None]}, "must_be_list_of_objects"),
            ({"market_windows": [extra_window], "cash_release_receipts": [capture["cash_release_receipts"][0]]}, "identity_reused"),
            ({"rows": []}, "capture_evidence_collections_invalid"),
            ({"orderfilled_records": []}, "capture_evidence_collections_invalid"),
            ({"closing_balance": "999"}, "capture_evidence_collections_invalid"),
        ):
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, expected):
                buffer.attach_evidence(invalid)
            self.assertEqual(buffer.build(), baseline)
