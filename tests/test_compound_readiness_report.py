from __future__ import annotations

from tools.compound_readiness_report import build_report


def paper_artifact(*, block_failure: bool = True) -> dict:
    block = {"split": "holdout_block_1", "first_ten_fills": 10, "first_ten_passed": True}
    if block_failure:
        block = {"split": "holdout_block_3", "first_ten_fills": 3, "first_ten_passed": False}
    row = {
        "selection_all_tick_depth_slippage_gate": True,
        "holdout_all_tick_depth_slippage_gate": True,
        "exact_ten_tick_depth_gate": True,
        "holdout_block_stress": [block],
    }
    return {
        "protocol": {
            "same_ledger": True,
            "required_fills": 10,
            "initial_cash_usd": 10.0,
            "minimum_order_notional_usd": 0.0,
            "minimum_order_shares": 5.0,
        },
        "gates": {"h30_move_uncertainty": row, "h38_move_uncertainty_midpath": row},
    }


def h508_paper_artifact(*, holdout_failure: bool = False) -> dict:
    return {
        "format": "old-clean-h508-cash-fraction-depth-tradeoff-v1",
        "holdout_gate_passed": not holdout_failure,
        "protocol": {
            "initial_cash_usd": 10.0,
            "required_fills": 10,
            "minimum_order_usd": 5.0,
            "minimum_order_shares": 5.0,
            "same_ledger_within_window": True,
            "no_cross_window_concatenation": True,
            "old_clean_dataset_only": True,
            "paper_only": True,
        },
        "cell_summaries": [{
            "slippage": 0.005,
            "latency_seconds": 2.0,
            "depth_haircut": 0.1,
            "selected_fraction": 0.5,
            "selection_passed": True,
            "holdout_all_ten": not holdout_failure,
            "holdout_min_fills": 10 if not holdout_failure else 9,
            "holdout_min_cash": 11.0,
        }],
    }


def state(*, bundle: bool = False, fills: int = 0, binding: bool = False, receipts: bool = False) -> dict:
    return {
        "actual_fills": fills,
        "same_ledger_compounding_rows": 10 if binding else 0,
        "live_orders_enabled": False,
        "receipt_boundary_status": {
            "authenticated_owner_scoped_bundle_available": bundle,
            "btc_five_minute_bundle_available": bundle,
            "verified_canary_fills": fills,
            "owner_scoped_settlement_rows": 10 if receipts else 0,
            "owner_scoped_cash_release_rows": 10 if receipts else 0,
            "owner_scoped_balance_checkpoint_rows": 10 if receipts else 0,
            "settlement_evidence_verified": receipts,
            "cash_release_evidence_verified": receipts,
            "balance_checkpoint_verified": receipts,
        },
        "latest_orderfilled_binding_audit": {
            "verification": {
                "current_owner_bundle_available": binding,
                "synthetic_only": not binding,
            },
            "decision": {
                "same_ledger_compounding_rows": 10 if binding else 0,
                "canary_ready": binding,
            },
            "evidence_boundary": "test_fixture",
        },
    }


def test_report_separates_pooled_prefix_from_block_stress_and_receipts():
    report = build_report(state(), paper_artifact())
    assert report["paper_frontier"]["pooled_frontier_consistent"] is True
    assert report["paper_frontier"]["block_stress_stable"] is False
    assert report["settlement_evidence"]["ready"] is False
    assert report["canary_ready"] is False
    assert "authenticated_owner_scoped_btc_five_minute_bundle_missing" in report["hard_blockers"]


def test_report_can_only_be_ready_with_ten_settled_fills_and_consistent_protocol():
    report = build_report(state(bundle=True, fills=10, binding=True, receipts=True), paper_artifact(block_failure=False))
    assert report["paper_frontier"]["pooled_frontier_consistent"] is True
    assert report["settlement_evidence"]["ready"] is True
    assert report["canary_ready"] is True


def test_block_stress_blocks_canary_admission_even_with_real_settlement_evidence():
    report = build_report(state(bundle=True, fills=10, binding=True, receipts=True), paper_artifact(block_failure=True))
    assert report["settlement_evidence"]["ready"] is True
    assert report["canary_ready"] is False
    assert report["paper_frontier"]["block_stress_stable"] is False
    assert "paper_block_stress_unstable" in report["hard_blockers"]


def test_report_fails_closed_when_binding_audit_is_absent():
    fixture = state(bundle=True, fills=10)
    fixture.pop("latest_orderfilled_binding_audit")
    report = build_report(fixture, paper_artifact(block_failure=False))
    assert report["orderfilled_binding"]["ready"] is False
    assert report["settlement_evidence"]["ready"] is False
    assert "onchain_orderfilled_binding_missing_or_unverified" in report["hard_blockers"]


def test_report_fails_closed_when_receipt_components_are_absent():
    report = build_report(state(bundle=True, fills=10, binding=True), paper_artifact(block_failure=False))
    assert report["orderfilled_binding"]["ready"] is True
    assert report["settlement_evidence"]["ready"] is False
    assert "settlement_receipts_missing_or_unverified" in report["hard_blockers"]
    assert "cash_release_receipts_missing_or_unverified" in report["hard_blockers"]
    assert "balance_checkpoint_receipts_missing_or_unverified" in report["hard_blockers"]


def test_report_fails_closed_when_same_ledger_row_count_is_not_exactly_ten():
    fixture = state(bundle=True, fills=10, binding=True, receipts=True)
    fixture["same_ledger_compounding_rows"] = 9
    report = build_report(fixture, paper_artifact(block_failure=False))
    assert report["orderfilled_binding"]["ready"] is True
    assert report["settlement_evidence"]["ready"] is False
    assert "same_ledger_compounding_rows_missing_or_not_exactly_ten" in report["hard_blockers"]


def test_report_fails_closed_when_binding_is_not_explicitly_non_synthetic():
    fixture = state(bundle=True, fills=10, binding=True)
    del fixture["latest_orderfilled_binding_audit"]["verification"]["synthetic_only"]
    report = build_report(fixture, paper_artifact(block_failure=False))
    assert report["orderfilled_binding"]["ready"] is False
    assert report["settlement_evidence"]["ready"] is False


def test_report_consumes_h508_old_clean_holdout_contract():
    report = build_report(state(), h508_paper_artifact())
    assert report["paper_frontier"]["candidate_schema"] == "old-clean-h508-cash-fraction-depth-tradeoff-v1"
    assert report["paper_frontier"]["pooled_frontier_consistent"] is True
    assert report["paper_frontier"]["block_stress_stable"] is True
    assert report["paper_frontier"]["selection_cells_stable"] is True
    assert report["canary_ready"] is False


def test_h508_holdout_failure_blocks_the_compound_gate():
    report = build_report(state(), h508_paper_artifact(holdout_failure=True))
    assert report["paper_frontier"]["pooled_frontier_consistent"] is False
    assert report["paper_frontier"]["block_stress_stable"] is False
    assert "paper_block_stress_unstable" in report["hard_blockers"]
